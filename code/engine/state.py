"""Stage 4: the state of each scoped resource at the case's audit_at.

  * Only evidence with captured_at <= audit_at counts; the newest such file
    wins (the cut-off uses the manifest capture time, not in-file timestamps).
  * Inside the chosen file every copy is compared separately. For SQL
    snapshots, a primary/replica table that covers the resource's system but
    lacks the row is an ABSENT copy, so a row surviving only in a replica shows
    up as drift. The resource takes the most exposed state among its copies.
  * With no direct observation, a snapshot whose coverage includes the system
    but not the resource proves absence: the newest collection snapshot or
    primary+replica SQL pair, falling back to single-scope inventories only
    when nothing else covers the system.
  * reported_status / reported_result are never used as state; activity rows
    are kept only as claims and as recreation signals.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from extract.common import PREFIXES, resource_prefix

from .evidence import EvidenceStore
from .scope import CaseScope, category_of

SEVERITY = {"PRESENT": 7, "ACTIVE": 6, "UNKNOWN": 5, "REDACTED": 4, "ANONYMIZED": 3,
            "DISABLED": 2, "BLOCKED": 1, "ABSENT": 0}
RECREATION_EVENTS = {"SYNC_RECORD", "RESTORE_BACKUP", "INDEX_RECORD", "REFRESH_CACHE", "CREATE_EXPORT"}
TREATMENT_EVENTS = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE"}


@dataclass
class ResourceState:
    resource_id: str
    system_id: str | None
    category: str | None
    state: str
    basis: str                              # observed | coverage_absence | no_evidence
    evidence_id: str | None
    captured_at: str | None
    copies: list[dict[str, Any]] = field(default_factory=list)
    drift: str = ""
    exposed: dict[str, list[str]] = field(default_factory=dict)
    history: list[tuple[str, str, str]] = field(default_factory=list)    # (file_id, captured_at, state)
    claims: list[dict[str, str]] = field(default_factory=list)
    later_evidence: list[str] = field(default_factory=list)              # files after audit_at (ignored)


def _file_rank(store: EvidenceStore, file_id: str, system: str) -> int:
    roles = {c["copy_role"] for c in store.coverage
             if c["file_id"] == file_id and system in c.get("system_ids", [])}
    if "snapshot" in roles:
        return 3
    if {"primary", "replica"} <= roles:
        return 2
    return 1


def coverage_absence(store: EvidenceStore, rid: str, system: str,
                     audit_at: str) -> tuple[str, str, list[dict[str, Any]]] | None:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in store.coverage:
        if c["captured_at"] <= audit_at and system in c.get("system_ids", []):
            by_file[c["file_id"]].append(c)
    candidates = [f for f, entries in by_file.items() if all(rid not in e["resource_ids"] for e in entries)]
    if not candidates:
        return None
    best = max(candidates, key=lambda f: (_file_rank(store, f, system) > 1, store.manifest[f]["captured_at"], f))
    copies = [{"copy": e["copy"], "copy_role": e["copy_role"], "state": "ABSENT", "locator": "not in snapshot"}
              for e in by_file[best]]
    return best, store.manifest[best]["captured_at"], copies


def resource_state(store: EvidenceStore, rid: str, audit_at: str) -> ResourceState:
    prefix = resource_prefix(rid)
    system = PREFIXES[prefix][0] if prefix else None
    category = category_of(store, rid)
    records = store.by_resource.get(rid, [])
    before = [r for r in records if r["captured_at"] <= audit_at]
    later = sorted({r["file_id"] for r in records if r["captured_at"] > audit_at})
    claims = [{"activity_id": a["activity_id"], "event_time": a["event_time"], "event_type": a["event_type"],
               "reported_result": a["reported_result"]} for a in store.activities_for(rid, audit_at)]

    history = []
    for f in sorted({r["file_id"] for r in before}, key=lambda f: (store.manifest[f]["captured_at"], f)):
        fr = [r for r in before if r["file_id"] == f]
        history.append((f, store.manifest[f]["captured_at"], max((r["state"] for r in fr), key=SEVERITY.get)))

    if before:
        file_id = max({r["file_id"] for r in before}, key=lambda f: (store.manifest[f]["captured_at"], f))
        in_file = [r for r in before if r["file_id"] == file_id]
        copies = [{"copy": r["copy"] or r["source_type"], "copy_role": r["copy_role"], "state": r["state"],
                   "locator": r["locator"]} for r in in_file]
        # SQL tables covering this system but lacking the row are absent copies.
        present_tables = {r["copy"] for r in in_file}
        for c in store.coverage:
            if (c["file_id"] == file_id and c["source_type"] == "sql" and system in c.get("system_ids", [])
                    and c["copy"] not in present_tables and rid not in c["resource_ids"]):
                copies.append({"copy": c["copy"], "copy_role": c["copy_role"], "state": "ABSENT",
                               "locator": "missing row"})
        state = max((c["state"] for c in copies), key=SEVERITY.get)
        absent = [c["copy"] for c in copies if c["state"] == "ABSENT"]
        kept = [c["copy"] for c in copies if c["state"] != "ABSENT"]
        drift = f"absent in {absent}, still in {kept}" if absent and kept else ""
        exposed: dict[str, list[str]] = defaultdict(list)
        for r in in_file:
            for kind, values in r["exposed"].items():
                for v in values:
                    if v not in exposed[kind]:
                        exposed[kind].append(v)
        category = next((r["category"] for r in in_file if r["category"]), category)
        return ResourceState(rid, system, category, state, "observed", file_id,
                             store.manifest[file_id]["captured_at"], copies, drift, dict(exposed), history,
                             claims, later)

    # Snapshots are partial: their silence proves absence only for something a deletion targeted,
    # never for records policy keeps (audit logs, legal holds, backups).
    targeted = any(c["event_type"] in TREATMENT_EVENTS for c in claims)
    retained = category in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE")
    if system and targeted and not retained and (absent := coverage_absence(store, rid, system, audit_at)):
        file_id, captured, copies = absent
        return ResourceState(rid, system, category, "ABSENT", "coverage_absence", file_id, captured, copies,
                             "", {}, history, claims, later)
    return ResourceState(rid, system, category, "UNKNOWN", "no_evidence", None, None, [], "", {}, history,
                         claims, later)


def recreation_risks(store: EvidenceStore, states: dict[str, ResourceState], audit_at: str) -> list[dict[str, str]]:
    """Processes that can copy or restore the person's data, as of audit_at."""
    risks = []
    for rid, st in sorted(states.items()):
        if st.category == "DATA_FLOW" and st.state == "ACTIVE":
            risks.append({"kind": "active_data_flow", "resource_id": rid, "evidence_id": st.evidence_id or ""})
        if st.category == "BACKUP_ARCHIVE" and st.state == "ACTIVE":
            risks.append({"kind": "restore_risk", "resource_id": rid, "evidence_id": st.evidence_id or ""})
        acts = store.activities_for(rid, audit_at)
        # Claims only (not proof): checks reporting a flow still enabled / a backup still restorable.
        for a in acts:
            details = a["details"].lower()
            if a["event_type"] == "CHECK_DATA_FLOW" and "remains enabled" in details and st.state != "ACTIVE":
                risks.append({"kind": "claimed_active_flow", "resource_id": rid, "evidence_id": a["activity_id"]})
            if a["event_type"] == "CHECK_RESTORE_ACCESS" and "can still be restored" in details and st.state != "ACTIVE":
                risks.append({"kind": "claimed_restorable", "resource_id": rid, "evidence_id": a["activity_id"]})
        # Copy / re-index / restore activity after the resource was treated can recreate it.
        treated = [a["event_time"] for a in acts if a["event_type"] in TREATMENT_EVENTS]
        if treated:
            for a in acts:
                if a["event_type"] in RECREATION_EVENTS and a["event_time"] > min(treated):
                    risks.append({"kind": f"post_treatment_{a['event_type'].lower()}", "resource_id": rid,
                                  "evidence_id": a["activity_id"]})
    return risks


def case_state(store: EvidenceStore, scope: CaseScope,
               audit_at: str) -> tuple[dict[str, ResourceState], list[dict[str, str]]]:
    states = {rid: resource_state(store, rid, audit_at) for rid in sorted(scope.resources)}
    return states, recreation_risks(store, states, audit_at)
