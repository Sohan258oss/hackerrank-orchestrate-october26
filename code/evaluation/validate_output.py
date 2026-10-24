"""Independent contract validator for output.csv (does not reuse the pipeline's own checks).

Usage (from the repository root):

    python code/evaluation/validate_output.py                 # validates ./output.csv
    python code/evaluation/validate_output.py --pred FILE

Errors are contract violations and make the exit code 1. Warnings are allowed but worth reviewing:
a sort exception caused by the downstream-copy-before-source rule, or a COMPLETE case that
cites no verification evidence.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HEADER = ["case_id", "deletion_status", "steps_json"]
STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
OPERATIONS = [   # problem statement order: (phase, operation, state)
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
]
ALLOWED = {(p, o): s for p, o, s in OPERATIONS}
OP_RANK = {o: i for i, (_, o, _) in enumerate(OPERATIONS)}
PHASE_RANK = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def case_num(case_id: str) -> int:
    return int(case_id.rsplit("_", 1)[1])


def evidence_times(dataset: Path) -> dict[str, str]:
    times = {r["file_id"]: r["captured_at"] for r in read(dataset / "evidence_files.csv")}
    times.update({r["activity_id"]: r["event_time"] for r in read(dataset / "system_activity.csv")})
    times.update({r["audit_id"]: r["event_time"] for r in read(dataset / "user_audit_log.csv")})
    return times


def validate(pred: Path, dataset: Path) -> tuple[Counter, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    with pred.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        rows = list(reader)
    if header != HEADER:
        errors.append(f"header is {header}, expected {HEADER}")

    requests = {r["case_id"]: r for r in read(dataset / "deletion_requests.csv")}
    ids = [r[0] for r in rows]
    if len(rows) != len(requests):
        errors.append(f"{len(rows)} data rows, expected {len(requests)}")
    if len(set(ids)) != len(ids):
        errors.append("duplicate case_id rows")
    if set(ids) != set(requests):
        errors.append(f"case_id set differs: missing {sorted(set(requests) - set(ids))[:5]}, "
                      f"extra {sorted(set(ids) - set(requests))[:5]}")
    if ids != sorted(ids, key=case_num):
        errors.append("rows are not sorted by case_id")

    times = evidence_times(dataset)
    statuses: Counter = Counter()
    for row in rows:
        if len(row) != 3:
            errors.append(f"{row[:1]}: {len(row)} columns")
            continue
        case_id, status, raw = row
        statuses[status] += 1
        err = lambda msg: errors.append(f"{case_id}: {msg}")  # noqa: E731
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            err(f"invalid deletion_status {status!r}")
        try:
            steps = json.loads(raw)
        except json.JSONDecodeError as exc:
            err(f"steps_json is not valid JSON ({exc})")
            continue
        if not isinstance(steps, list):
            err("steps_json is not a JSON array")
            continue
        if status == "NOT_APPLICABLE" and steps != []:
            err("NOT_APPLICABLE must have steps_json=[]")
        if status == "COMPLETE" and not steps:
            warnings.append(f"{case_id}: COMPLETE with no verification evidence")

        audit_at = requests.get(case_id, {}).get("audit_at", "")
        bad_shape = False
        for i, s in enumerate(steps, start=1):
            if not isinstance(s, dict) or set(s) != STEP_KEYS:
                err(f"step {i} keys are {sorted(s) if isinstance(s, dict) else type(s).__name__}")
                bad_shape = True
                continue
            if s["step_no"] != i:
                err(f"step_no {s['step_no']!r} at position {i} (must be consecutive from 1)")
            expected_state = ALLOWED.get((s["phase"], s["operation"]))
            if expected_state is None:
                err(f"step {i}: operation {s['operation']} not allowed in phase {s['phase']}")
            elif s["state"] != expected_state:
                err(f"step {i}: {s['operation']} must have state {expected_state}, got {s['state']}")
            if not s["system_id"] or not s["resource_id"]:
                err(f"step {i}: empty system_id or resource_id")

            ev = s["evidence_id"]
            if status == "COMPLETE":
                if s["phase"] != "VERIFICATION":
                    err(f"step {i}: COMPLETE cases may only contain VERIFICATION steps")
                if not ev:
                    err(f"step {i}: COMPLETE verification must cite evidence")
            elif status == "INCOMPLETE":
                if s["phase"] == "FINDING" and not ev:
                    err(f"step {i}: finding must cite evidence")
                if s["phase"] != "FINDING" and ev:
                    err(f"step {i}: future {s['phase']} step must have empty evidence_id")
            if ev:
                if ev not in times:
                    err(f"step {i}: evidence_id {ev} is not a participant-visible file/activity/audit id")
                elif times[ev] > audit_at:
                    err(f"step {i}: evidence {ev} at {times[ev]} is after audit_at {audit_at}")
        if bad_shape:
            continue

        # Ordering: phases, then operation-table order, then system_id, resource_id.
        phases = [PHASE_RANK.get(s["phase"], 9) for s in steps]
        if phases != sorted(phases):
            err("phases not ordered FINDING -> REMEDIATION -> VERIFICATION")
        ops = [(PHASE_RANK.get(s["phase"], 9), OP_RANK.get(s["operation"], 99)) for s in steps]
        if ops != sorted(ops):
            err("operations not in problem-statement order within a phase")
        keys = [(*o, s["system_id"], s["resource_id"]) for o, s in zip(ops, steps)]
        if keys != sorted(keys):
            moved = [s for k, s in zip(keys, steps) if s["phase"] == "REMEDIATION"]
            remediation_only = all(a <= b or steps[j]["phase"] == "REMEDIATION"
                                   for j, (a, b) in enumerate(zip(keys, keys[1:])))
            if moved and remediation_only:
                warnings.append(f"{case_id}: remediation deviates from system/resource sort "
                                "(downstream copy before its source)")
            else:
                err("steps not sorted by system_id, resource_id within an operation")
    return statuses, errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pred", type=Path, default=REPO_ROOT / "output.csv")
    parser.add_argument("--dataset", type=Path, default=REPO_ROOT / "dataset")
    args = parser.parse_args(argv)
    statuses, errors, warnings = validate(args.pred, args.dataset)
    print(f"{args.pred}: {sum(statuses.values())} rows; status distribution {dict(sorted(statuses.items()))}")
    print(f"violations: {len(errors)}")
    for e in errors:
        print(f"  ERROR {e}")
    print(f"warnings: {len(warnings)}")
    for w in warnings:
        print(f"  warn  {w}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
