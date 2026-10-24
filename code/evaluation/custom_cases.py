"""Participant-made evaluation cases (no model calls, no organizer data).

Usage (from the repository root):

    python code/evaluation/custom_cases.py

Each case builds a small, hand-written scenario and checks the agent's decision against the
answer derived by hand from problem_statement.md and deletion_rules.csv. Cases cover the parts
the 32 public samples exercise least: non-deletion intent (the samples contain no
NOT_APPLICABLE case), the audit_at evidence cutoff, account tombstones, remediation ordering
and recreation risks. Identity cases use the participant-facing identity files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from engine.identity import IdentityIndex, email_sha256  # noqa: E402
from engine.intent import rule_classify  # noqa: E402
from engine.policy import assess, order_steps, make_step, serialize  # noqa: E402
from engine.state import ResourceState  # noqa: E402
from extract.env import REPO_ROOT  # noqa: E402

RULES = {"CUSTOMER_PROFILE": "DELETE", "SUPPORT_TICKET": "REDACT", "ANALYTICS_EVENT": "ANONYMIZE",
         "INVOICE": "RETAIN"}


def fake_pipeline(files: dict[str, str], activity: list[dict[str, str]] | None = None,
                  by_resource: dict[str, list] | None = None) -> SimpleNamespace:
    """Minimal stand-in for engine.pipeline.Pipeline: evidence manifest, activity rows, records."""
    manifest = {fid: {"captured_at": at} for fid, at in files.items()}
    return SimpleNamespace(store=SimpleNamespace(manifest=manifest, activity=activity or [],
                                                 by_resource=by_resource or {}))


def state(rid: str, system: str, category: str, observed: str, evidence: str) -> ResourceState:
    return ResourceState(rid, system, category, observed, "observed", evidence, None)


def steps_for(assessments, deps=None) -> list[dict]:
    """Same step construction as engine.policy.decide for an INCOMPLETE case."""
    steps = []
    violations = [a for a in assessments if a.finding]
    for a in violations:
        steps.append(make_step("FINDING", a, a.finding, a.evidence_id))
        steps.append(make_step("REMEDIATION", a, a.remediation, ""))
    for a in assessments:
        steps.append(make_step("VERIFICATION", a, a.verification, ""))
    return order_steps(steps, deps or {})


def case_intent() -> list[tuple[str, object, object]]:
    texts = {
        "negated deletion + export (EN)": ("Please don't delete my account. I only need a copy of my order history.", False),
        "deletion request (ES)": ("Por favor, eliminen mi cuenta y todos mis datos personales.", True),
        "marketing opt-out (EN)": ("Stop sending me marketing emails, unsubscribe me from the newsletter.", False),
        "deletion request (DE)": ("Bitte löschen Sie mein Konto und alle meine Daten.", True),
        # Non-English non-deletion requests have no rule: they must be deferred to the LLM (None), not guessed.
        "correction (FR) deferred to LLM": ("Pouvez-vous corriger mon adresse e-mail dans mon profil ?", None),
    }
    return [(name, expected, rule_classify(text).authorizes_deletion) for name, (text, expected) in texts.items()]


def case_identity() -> list[tuple[str, object, object]]:
    index = IdentityIndex(REPO_ROOT / "dataset")
    out = []
    # A user whose email changed: the old address and its hash must still resolve to them.
    uid, row = next((u, r) for u, rows in sorted(index.changes.items()) for r in rows if r["field_name"] == "email")
    old = row["old_value"]
    res = index.resolve(old.upper())
    out.append(("historical email (case-insensitive) resolves", uid, res.identity.user_id if res.identity else None))
    out.append(("old email hash kept as identifier", True,
                bool(res.identity) and email_sha256(old) in res.identity.hashes))
    out.append(("unknown email does not resolve", "not_found", index.resolve("nobody@example.invalid").status))
    return out


def case_cutoff() -> list[tuple[str, object, object]]:
    # The only proof the profile is still present was captured after audit_at: it must not be cited.
    pipe = fake_pipeline({"evidence_late": "2026-07-02T09:00:00Z", "evidence_early": "2026-06-01T09:00:00Z"})
    audit_at = "2026-07-01T00:00:00Z"
    late = assess(state("account_1", "account_service", "CUSTOMER_PROFILE", "PRESENT", "evidence_late"),
                  [], RULES, pipe, audit_at)
    early = assess(state("account_1", "account_service", "CUSTOMER_PROFILE", "PRESENT", "evidence_early"),
                   [], RULES, pipe, audit_at)
    return [("evidence after audit_at gives no finding", None, late.finding),
            ("evidence before audit_at gives a finding", ("REPORT_REMAINING_RECORD", "evidence_early"),
             (early.finding, early.evidence_id))]


def case_tombstone_and_retain() -> list[tuple[str, object, object]]:
    pipe = fake_pipeline({"evidence_a": "2026-06-01T00:00:00Z"})
    at = "2026-07-01T00:00:00Z"
    tomb_exposed = assess(state("account_9", "account_service", "ACCOUNT_TOMBSTONE", "PRESENT", "evidence_a"),
                          [], RULES, pipe, at)
    tomb_ok = assess(state("account_9", "account_service", "ACCOUNT_TOMBSTONE", "REDACTED", "evidence_a"),
                     [], RULES, pipe, at)
    invoice = assess(state("invoice_3", "billing", "INVOICE", "PRESENT", "evidence_a"), [], RULES, pipe, at)
    invoice_gone = assess(state("invoice_3", "billing", "INVOICE", "ABSENT", "evidence_a"), [], RULES, pipe, at)
    return [("tombstone with identifiers -> redact, not delete", ("REPORT_EXPOSED_PII", "REDACT_RECORD"),
             (tomb_exposed.finding, tomb_exposed.remediation)),
            ("redacted tombstone verified as REDACTED", ("VERIFY_RECORD_REDACTED", True),
             (tomb_ok.verification, tomb_ok.verified)),
            ("retained invoice is never deleted", ("VERIFY_RECORD_PRESENT", None),
             (invoice.verification, invoice.remediation)),
            ("missing retained invoice makes the case incomplete", True, invoice_gone.retained_missing)]


def case_ordering() -> list[tuple[str, object, object]]:
    # An active sync recreates the profile; a search document is a downstream copy of the profile.
    pipe = fake_pipeline({"evidence_a": "2026-06-01T00:00:00Z"})
    at = "2026-07-01T00:00:00Z"
    rules = dict(RULES, SEARCH_DOCUMENT="DELETE")
    assessments = [
        assess(state("account_7", "account_service", "CUSTOMER_PROFILE", "PRESENT", "evidence_a"), [], rules, pipe, at),
        assess(state("search_7", "search_index", "SEARCH_DOCUMENT", "PRESENT", "evidence_a"), [], rules, pipe, at),
        assess(state("sync_7", "crm_sync", "DATA_FLOW", "ACTIVE", "evidence_a"), [], rules, pipe, at),
        assess(state("backup_7", "backup_vault", "BACKUP_ARCHIVE", "ACTIVE", "evidence_a"), [], rules, pipe, at),
    ]
    steps = steps_for(assessments, deps={"search_7": "account_7"})
    remediation = [(s["operation"], s["resource_id"]) for s in steps if s["phase"] == "REMEDIATION"]
    phases = [s["phase"] for s in steps]
    return [("flow disabled, restore blocked, copy before source",
             [("DISABLE_DATA_FLOW", "sync_7"), ("BLOCK_RESTORE", "backup_7"),
              ("DELETE_RECORD", "search_7"), ("DELETE_RECORD", "account_7")], remediation),
            ("phases ordered and steps numbered from 1", True,
             phases == sorted(phases, key=["FINDING", "REMEDIATION", "VERIFICATION"].index)
             and [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))),
            ("future steps carry empty evidence_id", True,
             all(s["evidence_id"] == "" for s in steps if s["phase"] != "FINDING")),
            ("serialization is valid JSON", len(steps), len(json.loads(serialize(steps))))]


CASES = {
    "C1 intent classification beyond the samples": case_intent,
    "C2 identity resolution through history": case_identity,
    "C3 audit_at evidence cutoff": case_cutoff,
    "C4 tombstone and retained records": case_tombstone_and_retain,
    "C5 remediation ordering and recreation risks": case_ordering,
}


def main() -> int:
    total = passed = 0
    for title, fn in CASES.items():
        print(title)
        for name, expected, actual in fn():
            ok = expected == actual
            total += 1
            passed += ok
            print(f"  [{'pass' if ok else 'FAIL'}] {name}" + ("" if ok else f": expected {expected!r}, got {actual!r}"))
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
