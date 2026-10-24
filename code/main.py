"""Ghost Data Still Exists: deletion-audit agent.

Usage (from the repository root):

    python code/main.py                     # all 518 evaluation cases -> ./output.csv
    python code/main.py --set sample --out .cache/evaluation/sample_pred.csv
    python code/main.py --skip-extract      # reuse .cache/extract/records.jsonl as is
    python code/main.py --no-llm            # intent rules only, no model calls

Pipeline:
  0. extract    normalize every evidence file into records (cached by file hash;
                screenshots use the vision model when a key is configured)
  1. intent     does the request authorize deletion?  (rules, LLM fallback)
  2. identity   current and historical identifiers of the person
  3. scope      the case's resources (deletion batch, identifier matches, retained records)
  4. state      each resource's state at audit_at (newest evidence, replica drift, recreation risks)
  5. policy     required treatment per deletion_rules.csv; COMPLETE / INCOMPLETE / NOT_APPLICABLE
  6. steps      ordered FINDING / REMEDIATION / VERIFICATION steps with evidence ids
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.pipeline import case_sort_key, prepare  # noqa: E402
from engine.policy import decide, load_rules, serialize  # noqa: E402
from extract import llm  # noqa: E402
from extract import run as extract_run  # noqa: E402
from extract.env import CACHE_ROOT, REPO_ROOT  # noqa: E402

HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
PHASE_RANK = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}


def validate(rows: list[list[str]], expected_ids: list[str]) -> list[str]:
    """Contract checks from the problem statement; returns a list of problems (empty when valid)."""
    problems = []
    if [r[0] for r in rows] != expected_ids:
        problems.append("case ids do not match the request file one-to-one in sorted order")
    for case_id, status, steps_json in rows:
        if status not in STATUSES:
            problems.append(f"{case_id}: bad status {status}")
        steps = json.loads(steps_json)
        if status == "NOT_APPLICABLE" and steps:
            problems.append(f"{case_id}: NOT_APPLICABLE with steps")
        if [s["step_no"] for s in steps] != list(range(1, len(steps) + 1)):
            problems.append(f"{case_id}: step numbers not consecutive from 1")
        ranks = [PHASE_RANK[s["phase"]] for s in steps]
        if ranks != sorted(ranks):
            problems.append(f"{case_id}: phases out of order")
        for s in steps:
            future = s["phase"] != "FINDING" and status == "INCOMPLETE"
            if future and s["evidence_id"]:
                problems.append(f"{case_id}: step {s['step_no']} should have empty evidence_id")
            if not future and not s["evidence_id"]:
                problems.append(f"{case_id}: step {s['step_no']} is missing its evidence_id")
    return problems


def run_usage(run_id: str) -> dict[str, dict[str, int]]:
    if not llm.USAGE_LOG.is_file():
        return {}
    totals: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for line in llm.USAGE_LOG.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["run_id"] != run_id:
            continue
        key = f"{row['provider']}/{row['model']}:{row['prompt']}"
        totals[key]["calls"] += 0 if row["cached"] else 1
        totals[key]["cache_hits"] += 1 if row["cached"] else 0
        totals[key]["input_tokens"] += row["input_tokens"]
        totals[key]["output_tokens"] += row["output_tokens"]
    return {k: dict(v) for k, v in totals.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=REPO_ROOT / "dataset")
    parser.add_argument("--set", choices=["eval", "sample"], default="eval")
    parser.add_argument("--out", type=Path, default=None, help="default: ./output.csv for eval")
    parser.add_argument("--skip-extract", action="store_true", help="use the existing extraction output")
    parser.add_argument("--no-llm", action="store_true", help="intent rules only")
    parser.add_argument("--exclude-ambiguous", action="store_true",
                        help="drop batch resources whose case assignment was a tie")
    parser.add_argument("--debug", type=Path, default=None, help="write per-case decisions and notes as JSONL")
    args = parser.parse_args(argv)
    out = args.out or (REPO_ROOT / "output.csv" if args.set == "eval"
                       else CACHE_ROOT / "evaluation" / "sample_pred.csv")
    started = time.monotonic()

    if not args.skip_extract or not (CACHE_ROOT / "extract" / "records.jsonl").is_file():
        print("[0/6] extracting evidence (cached by file hash)...", flush=True)
        extract_run.main(["--dataset", str(args.dataset)])

    print("[1-4/6] intent, identity, scoping, state...", flush=True)
    pipeline = prepare(args.dataset, use_llm=not args.no_llm)
    rules = load_rules(args.dataset)

    print("[5-6/6] policy and steps...", flush=True)
    case_ids = sorted((r["case_id"] for r in pipeline.requests[args.set]), key=case_sort_key)
    rows, debug = [], []
    for case_id in case_ids:
        d = decide(case_id, pipeline, rules, include_ambiguous=not args.exclude_ambiguous)
        rows.append([case_id, d.status, serialize(d.steps)])
        debug.append({"case_id": case_id, "status": d.status, "notes": d.notes,
                      "assessments": [vars(a) for a in d.assessments]})

    problems = validate(rows, case_ids)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)   # RFC 4180 quoting, CRLF line ends like the template
        writer.writerow(HEADER)
        writer.writerows(rows)
    if args.debug:
        args.debug.parent.mkdir(parents=True, exist_ok=True)
        with args.debug.open("w", encoding="utf-8", newline="\n") as fh:
            for item in debug:
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    statuses = collections.Counter(r[1] for r in rows)
    n_steps = [len(json.loads(r[2])) for r in rows]
    print(f"wrote {out}: {len(rows)} rows; {dict(sorted(statuses.items()))}; "
          f"steps per case avg={sum(n_steps) / max(len(rows), 1):.1f} max={max(n_steps, default=0)}; "
          f"{time.monotonic() - started:.0f}s")
    print(f"model usage this run: {run_usage(llm.RUN_ID) or 'none (all cached or rules-only)'}")
    if problems:
        print(f"CONTRACT PROBLEMS ({len(problems)}):")
        for p in problems[:30]:
            print(f"  {p}")
        return 1
    print("contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
