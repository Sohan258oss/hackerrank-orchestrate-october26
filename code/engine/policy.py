"""Stages 5-6: apply deletion_rules.csv to each resource state and build steps_json.

Treatments (from deletion_rules.csv; two are named here for clarity):
  DELETE     record must be gone                         -> ABSENT
  REDACT     record kept, direct identifiers removed      -> REDACTED
  ANONYMIZE  record kept, no longer linkable to the user   -> ANONYMIZED
  RETAIN     record must still exist                      -> PRESENT
  TOMBSTONE  ACCOUNT_TOMBSTONE: keep the stable user_id, remove name, email,
             phone and email_hash (the REDACT rule)        -> REDACTED
  BACKUP     BACKUP_ARCHIVE is retained but its restore must be blocked -> BLOCKED
  FLOW       a connector / data flow must be inactive     -> DISABLED

A case is COMPLETE only when every scoped record has its required treatment,
every retained record still exists, and no flow or restore path is active.

Steps: FINDING -> REMEDIATION -> VERIFICATION, each sorted by the operation
table order, then system_id, then resource_id; in remediation, flows are
disabled and restores blocked first (operation order), and a downstream copy
is moved ahead of its source when evidence names the dependency. Findings and
COMPLETE verifications cite the file / activity that proves the state at
audit_at; INCOMPLETE remediation and verification steps use "".
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pipeline import Pipeline
from .state import ResourceState

OPERATIONS = [   # operation-table order; defines sort order inside each phase
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
OP_STATE = {op: state for _, op, state in OPERATIONS}
OP_RANK = {op: i for i, (_, op, _) in enumerate(OPERATIONS)}
PHASE_RANK = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}

# treatment -> (finding, remediation, verification, compliant observed states)
TREATMENT_STEPS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT", {"ABSENT"}),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED", {"REDACTED"}),
    "TOMBSTONE": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED", {"REDACTED"}),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED", {"ANONYMIZED"}),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT", {"PRESENT", "REDACTED", "ANONYMIZED"}),
    "BACKUP": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED", {"BLOCKED"}),
    "FLOW": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED", {"DISABLED"}),
}
VIOLATING = {
    "DELETE": {"PRESENT", "REDACTED", "ANONYMIZED", "ACTIVE", "DISABLED", "BLOCKED"},   # any surviving copy
    "REDACT": {"PRESENT"}, "TOMBSTONE": {"PRESENT"}, "ANONYMIZE": {"PRESENT"},
    "RETAIN": set(), "BACKUP": {"ACTIVE"}, "FLOW": {"ACTIVE"},
}
STEP_KEYS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load_rules(dataset: Path) -> dict[str, str]:
    with (dataset / "deletion_rules.csv").open(encoding="utf-8", newline="") as fh:
        return {r["data_category"]: r["required_action"] for r in csv.DictReader(fh)}


def treatment_for(category: str | None, rules: dict[str, str]) -> str | None:
    if category == "ACCOUNT_TOMBSTONE":
        return "TOMBSTONE"
    if category == "BACKUP_ARCHIVE":
        return "BACKUP"
    if category == "DATA_FLOW":
        return "FLOW"
    return rules.get(category or "")


@dataclass
class Assessment:
    resource_id: str
    system_id: str
    category: str | None
    treatment: str
    state: str
    finding: str | None = None
    remediation: str | None = None
    verification: str | None = None
    evidence_id: str = ""             # proof of the observed state at audit_at
    verified: bool = False            # observed state is compliant and citable
    retained_missing: bool = False
    note: str = ""


@dataclass
class Decision:
    case_id: str
    status: str
    steps: list[dict[str, Any]]
    assessments: list[Assessment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def citable(pipeline: Pipeline, evidence_id: str | None, audit_at: str) -> str:
    """Return the id only if it existed at audit_at (file captured / activity logged by then)."""
    if not evidence_id:
        return ""
    store = pipeline.store
    if evidence_id in store.manifest:
        return evidence_id if store.manifest[evidence_id]["captured_at"] <= audit_at else ""
    activity = next((a for a in store.activity if a["activity_id"] == evidence_id), None)
    if activity:
        return evidence_id if activity["event_time"] <= audit_at else ""
    return ""


def assess(st: ResourceState, risks: list[dict[str, str]], rules: dict[str, str], pipeline: Pipeline,
           audit_at: str) -> Assessment | None:
    treatment = treatment_for(st.category, rules)
    if not treatment or not st.system_id:
        return None
    finding, remediation, verification, compliant = TREATMENT_STEPS[treatment]
    a = Assessment(st.resource_id, st.system_id, st.category, treatment, st.state, verification=verification)
    evidence = citable(pipeline, st.evidence_id, audit_at)

    state = st.state
    if state == "UNKNOWN" and treatment in ("FLOW", "BACKUP"):
        # No observation: an activity check reporting the path still open is the best proof available.
        kind = "claimed_active_flow" if treatment == "FLOW" else "claimed_restorable"
        claim = next((r for r in risks if r["kind"] == kind and r["resource_id"] == st.resource_id), None)
        if claim and (claim_evidence := citable(pipeline, claim["evidence_id"], audit_at)):
            state, evidence, a.note = "ACTIVE", claim_evidence, f"state from activity claim {claim['evidence_id']}"
    a.state = state

    if state in VIOLATING[treatment] and evidence:
        a.finding, a.remediation, a.evidence_id = finding, remediation, evidence
    elif state in compliant and evidence:
        a.verified, a.evidence_id = True, evidence
    elif state == "ABSENT" and treatment in ("REDACT", "TOMBSTONE", "ANONYMIZE"):
        a.verification = "VERIFY_RECORD_ABSENT"     # gone entirely: stronger than the required treatment
        a.verified, a.evidence_id = bool(evidence), evidence
    elif state == "ABSENT" and treatment == "RETAIN":
        a.retained_missing, a.evidence_id = True, evidence
    return a


def downstream_sources(pipeline: Pipeline, rids: set[str]) -> dict[str, str]:
    """copy -> source, when evidence names the source record of a derived copy (e.g. a search document)."""
    deps = {}
    for rid in rids:
        for record in pipeline.store.by_resource.get(rid, []):
            for key, value in record["attributes"].items():
                if key.rsplit(".", 1)[-1] in ("source_resource_id", "source_resource") and value in rids and value != rid:
                    deps[rid] = value
    return deps


def order_steps(steps: list[dict[str, Any]], deps: dict[str, str]) -> list[dict[str, Any]]:
    steps = sorted(steps, key=lambda s: (PHASE_RANK[s["phase"]], OP_RANK[s["operation"]],
                                         s["system_id"], s["resource_id"]))
    # Downstream copies before their sources, within the same remediation operation.
    moved = True
    while moved:
        moved = False
        for i, s in enumerate(steps):
            source = deps.get(s["resource_id"])
            if s["phase"] != "REMEDIATION" or not source:
                continue
            j = next((k for k, t in enumerate(steps) if t["resource_id"] == source
                      and t["phase"] == "REMEDIATION" and t["operation"] == s["operation"]), None)
            if j is not None and j < i:
                steps.insert(j, steps.pop(i))
                moved = True
                break
    for n, s in enumerate(steps, start=1):
        s["step_no"] = n
    return steps


def make_step(phase: str, a: Assessment, op: str, evidence_id: str) -> dict[str, Any]:
    return {"step_no": 0, "phase": phase, "system_id": a.system_id, "resource_id": a.resource_id,
            "operation": op, "state": OP_STATE[op], "evidence_id": evidence_id}


def decide(case_id: str, pipeline: Pipeline, rules: dict[str, str], include_ambiguous: bool = True) -> Decision:
    request = pipeline.by_case[case_id]
    intent = pipeline.intents[case_id]
    if intent.authorizes_deletion is False:
        return Decision(case_id, "NOT_APPLICABLE", [], notes=[f"intent: {intent.request_type} via {intent.source}"])
    if case_id not in pipeline.scopes:
        status = pipeline.resolutions[case_id].status
        return Decision(case_id, "INCOMPLETE", [], notes=[f"identity {status}; no records could be scoped"])

    audit_at = request["audit_at"]
    scope = pipeline.scopes[case_id]
    states, risks = pipeline.state(case_id)
    notes: list[str] = []
    assessments: list[Assessment] = []
    for rid, st in sorted(states.items()):
        if not include_ambiguous and rid in scope.ambiguous:
            notes.append(f"{rid}: dropped (ambiguous batch assignment)")
            continue
        a = assess(st, risks, rules, pipeline, audit_at)
        if a is None:
            notes.append(f"{rid}: no policy category ({st.category})")
            continue
        assessments.append(a)

    violations = [a for a in assessments if a.finding]
    incomplete = bool(violations) or any(a.retained_missing for a in assessments)
    steps: list[dict[str, Any]] = []
    if incomplete:
        for a in violations:
            steps.append(make_step("FINDING", a, a.finding, a.evidence_id))
            steps.append(make_step("REMEDIATION", a, a.remediation, ""))
        for a in assessments:
            steps.append(make_step("VERIFICATION", a, a.verification, ""))
    else:
        for a in assessments:
            if a.verified:
                steps.append(make_step("VERIFICATION", a, a.verification, a.evidence_id))
            else:
                notes.append(f"{a.resource_id}: state {a.state} not citable at audit_at; verification omitted")
    deps = downstream_sources(pipeline, {a.resource_id for a in assessments})
    return Decision(case_id, "INCOMPLETE" if incomplete else "COMPLETE", order_steps(steps, deps), assessments, notes)


def serialize(steps: list[dict[str, Any]]) -> str:
    """Deterministic JSON: fixed key order, compact separators, UTF-8 text."""
    return json.dumps([{k: s[k] for k in STEP_KEYS} for s in steps], ensure_ascii=False, separators=(",", ":"))
