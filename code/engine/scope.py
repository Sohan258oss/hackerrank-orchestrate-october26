"""Stage 3: which resources does each deletion case cover?

Sources, combined per case:
  batch      deletion-worker activity around the case's reported_at, kept when
             evidence attributes the resource to the case's person;
  batch_elimination
             batch resources no evidence attributes to anyone, assigned when
             exactly one case sharing the batch can claim them (ties are broken
             by batch order and reported as ambiguous);
  identity   resources whose evidence matches the person's identifiers and was
             captured inside [requested_at, audit_at];
  retained   the person's security-audit and legal-hold records (retained by
             policy, verified as present).

Exclusions: resources attributed only to other people (decoys, look-alikes),
and resources that belong to another deletion case of the same person (they
sit in that case's batch, or were seen only outside this case's window).

All cases are scoped together so that shared batches can be divided.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from extract.common import PREFIXES, RESOURCE_RE, resource_prefix

from .evidence import EvidenceStore, ts
from .identity import Identity

BATCH_BEFORE = timedelta(minutes=120)   # primary-profile deletes run ~45-75 min before reported_at
BATCH_AFTER = timedelta(minutes=180)    # failed actions and checks report up to ~2 h after it
RETAINED_CATEGORIES = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}
OFFSET_TOLERANCE = 20.0                 # minutes a contested row may sit from its template's learned offset
TIE_MARGIN = 1.0                        # minutes; closer scores than this are a tie
MIN_STATUS_EVIDENCE = 30                # attributed rows needed before a status->result binding is trusted
TIGHT_SPREAD = 5.0                      # row shapes timed this tightly (std dev, minutes) measure a case's delay
MIN_SAMPLES = 5                         # ... once seen at least this often


def templates(a: dict[str, str]) -> list[str]:
    """Activity row shapes without ids, most specific first: rows of one shape run at a
    fixed offset from reported_at. Coarser shapes stand in when a shape was never attributed."""
    details = re.sub(r"\d+", "#", RESOURCE_RE.sub("<R>", a["details"]))
    return [f"{a['event_type']}|{a['reported_result']}|{details}",
            f"*|{a['reported_result']}|{details}"]


def offset_minutes(a: dict[str, str], reported_at: str) -> float:
    return (ts(a["event_time"]) - ts(reported_at)).total_seconds() / 60


@dataclass
class Scoped:
    resource_id: str
    sources: list[str] = field(default_factory=list)
    activity_ids: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class CaseScope:
    case_id: str
    user_id: str
    window: tuple[str, str]
    resources: dict[str, Scoped] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)
    ambiguous: list[str] = field(default_factory=list)

    def add(self, rid: str, source: str, owners: set[str], activity_id: str | None = None, note: str = "") -> None:
        item = self.resources.setdefault(rid, Scoped(rid, owners=sorted(owners)))
        if source not in item.sources:
            item.sources.append(source)
        if activity_id and activity_id not in item.activity_ids:
            item.activity_ids.append(activity_id)
        if note:
            item.note = note


def category_of(store: EvidenceStore, rid: str) -> str | None:
    cats = [r["category"] for r in store.by_resource.get(rid, []) if r["category"]]
    if cats:
        return max(sorted(set(cats)), key=cats.count)
    prefix = resource_prefix(rid)
    return PREFIXES[prefix][1] if prefix else None


def build_scopes(requests: list[dict[str, str]], identities: dict[str, Identity],
                 store: EvidenceStore) -> dict[str, CaseScope]:
    """Scope every case in `identities` (deletion cases with a resolved person)."""
    req = {r["case_id"]: r for r in requests if r["case_id"] in identities}
    scopes = {c: CaseScope(c, identities[c].user_id, (r["requested_at"], r["audit_at"])) for c, r in req.items()}
    cases_of_user: dict[str, list[str]] = defaultdict(list)
    for c in req:
        cases_of_user[identities[c].user_id].append(c)

    # (a) deletion batches
    batch: dict[str, list[dict[str, str]]] = {}
    claim: dict[str, list[str]] = defaultdict(list)          # unattributed resource -> cases whose batch holds it
    batch_case_of: dict[str, set[str]] = defaultdict(set)     # resource -> cases whose batch holds it
    for c, r in req.items():
        reported = ts(r["reported_at"])
        batch[c] = store.deletion_batch(reported - BATCH_BEFORE, reported + BATCH_AFTER)
        uid = identities[c].user_id
        for a in batch[c]:
            rid = a["resource_id"]
            batch_case_of[rid].add(c)
            owners = store.owners(rid)
            if uid in owners:
                scopes[c].add(rid, "batch", owners, a["activity_id"])
            elif owners:
                scopes[c].excluded.setdefault(rid, f"attributed to {sorted(owners)}")
            elif c not in claim[rid]:
                claim[rid].append(c)

    # Unattributed batch resources: give each to the claimant whose schedule it fits (each row
    # shape runs at a learned offset from reported_at), at most one resource of a type per case.
    offsets: dict[str, list[float]] = defaultdict(list)
    for c, r in req.items():
        for a in batch[c]:
            if a["resource_id"] in scopes[c].resources:
                for t in templates(a):
                    offsets[t].append(offset_minutes(a, r["reported_at"]))
    learned = {t: statistics.median(v) for t, v in offsets.items()}

    first_seen: dict[tuple[str, str], dict[str, str]] = {}
    for c in req:
        for a in batch[c]:
            first_seen.setdefault((c, a["resource_id"]), a)

    def anchor(c: str) -> str:
        own = [a["event_time"] for a in batch[c]
               if a["resource_id"] in scopes[c].resources and resource_prefix(a["resource_id"]) == "account"]
        return min(own) if own else "9999"

    def fit(c: str, rid: str) -> float:
        """Minutes between the row's offset for case c and its shape's learned offset (inf if unknown)."""
        a = first_seen[(c, rid)]
        expected = next((learned[t] for t in templates(a) if t in learned), None)
        return abs(offset_minutes(a, req[c]["reported_at"]) - expected) if expected is not None else float("inf")

    # The workflow's reported_status summarizes some batch rows' results. Learn, per row kind, whether
    # status determines the result (on attributed rows); where it does, a contested row can only belong
    # to a case whose status is consistent with the row's result.
    def row_kind(a: dict[str, str]) -> str:
        """Action plus exact wording (ids masked), independent of the reported result."""
        return f"{a['event_type']}|{RESOURCE_RE.sub('<R>', a['details'])}"

    outcomes: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    seen: dict[str, int] = defaultdict(int)
    for c, r in req.items():
        for a in batch[c]:
            if a["resource_id"] in scopes[c].resources:
                outcomes[row_kind(a)][r["reported_status"]].add(a["reported_result"])
                seen[row_kind(a)] += 1
    status_bound = {kind for kind, by_status in outcomes.items()
                    if seen[kind] >= MIN_STATUS_EVIDENCE and all(len(v) == 1 for v in by_status.values())}

    def consistent(c: str, rid: str) -> bool:
        a = first_seen[(c, rid)]
        kind = row_kind(a)
        if kind not in status_bound:
            return True
        expected = outcomes[kind].get(req[c]["reported_status"])
        return expected is None or a["reported_result"] in expected

    def assign(rid: str, winner: str, tied: list[str]) -> None:
        scopes[winner].add(rid, "batch_elimination", set(), first_seen[(winner, rid)]["activity_id"],
                           note=f"no identifier evidence; tied claimants {sorted(tied)}" if len(tied) > 1 else "")
        if len(tied) > 1:
            scopes[winner].ambiguous.append(rid)
        for c in claim[rid]:
            if c != winner:
                scopes[c].excluded.setdefault(rid, f"unattributed; assigned to {winner}")

    def learn_stats() -> dict[str, tuple[float, float, int]]:
        """Row shape -> (median offset, spread, count) in minutes over rows currently in scope."""
        values: dict[str, list[float]] = defaultdict(list)
        for c, r in req.items():
            for a in batch[c]:
                if a["resource_id"] in scopes[c].resources:
                    values[templates(a)[0]].append(offset_minutes(a, r["reported_at"]))
        return {t: (statistics.median(v), statistics.pstdev(v), len(v)) for t, v in values.items()}

    def schedule_fit(c: str, rid: str, stats: dict[str, tuple[float, float, int]]) -> float:
        """Distance from where case c's own schedule (learned offset + case delay) puts this row.
        A case without tightly timed rows has no measured delay: its fit gets the row shape's spread."""
        a = first_seen[(c, rid)]
        row = stats.get(templates(a)[0])
        if row is None:
            return fit(c, rid)
        residuals = [offset_minutes(x, req[c]["reported_at"]) - stats[templates(x)[0]][0]
                     for x in batch[c] if x["resource_id"] in scopes[c].resources
                     and templates(x)[0] in stats and stats[templates(x)[0]][1] <= TIGHT_SPREAD
                     and stats[templates(x)[0]][2] >= MIN_SAMPLES]
        delay = statistics.median(residuals) if residuals else None
        residual = abs(offset_minutes(a, req[c]["reported_at"]) - row[0] - (delay or 0.0))
        return residual if delay is not None else residual + row[1]

    deferred: list[str] = []
    contested = sorted(claim, key=lambda rid: min(first_seen[(c, rid)]["event_time"] for c in claim[rid]))
    for rid in contested:
        prefix = resource_prefix(rid)
        open_slot = [c for c in claim[rid]
                     if not any(resource_prefix(x) == prefix for x in scopes[c].resources) and consistent(c, rid)]
        cands = [c for c in open_slot if fit(c, rid) <= OFFSET_TOLERANCE]
        if not cands and len(open_slot) == 1 and fit(open_slot[0], rid) == float("inf"):
            cands = open_slot   # no schedule knowledge and no competition
        if not cands:
            for c in claim[rid]:
                scopes[c].excluded.setdefault(rid, "unattributed; no claimant fits its schedule or slot")
            continue
        cands.sort(key=lambda c: (fit(c, rid), anchor(c), c))
        best = fit(cands[0], rid)
        tied = [c for c in cands if fit(c, rid) == best or fit(c, rid) - best <= TIE_MARGIN]
        if len(tied) > 1:
            deferred.append(rid)     # resolved below, once every clear-cut row is placed
            continue
        assign(rid, tied[0], tied)

    # Ties: cases sharing reported_at have the same global schedule, but each case's completed actions
    # run on a tight schedule of their own (a per-case start delay). Relearn offsets from every row placed
    # so far, measure each case's delay from its own tightly timed rows, and break ties by that.
    stats = learn_stats()
    for rid in deferred:
        prefix = resource_prefix(rid)
        cands = [c for c in claim[rid]
                 if not any(resource_prefix(x) == prefix for x in scopes[c].resources)
                 and consistent(c, rid) and fit(c, rid) <= OFFSET_TOLERANCE]
        if not cands:
            for c in claim[rid]:
                scopes[c].excluded.setdefault(rid, "unattributed; no claimant fits its schedule or slot")
            continue
        cands.sort(key=lambda c: (schedule_fit(c, rid, stats), anchor(c), c))
        assign(rid, cands[0], cands)

    for c, r in req.items():
        ident = identities[c]
        uid = ident.user_id
        values = ident.identifiers()
        siblings = [s for s in cases_of_user[uid] if s != c]
        for rid in sorted(store.resources_by_owner.get(uid, set())):
            if rid in scopes[c].resources:
                continue
            owners = store.owners(rid)
            in_sibling_batch = batch_case_of.get(rid, set()) & set(siblings)
            if in_sibling_batch:
                scopes[c].excluded.setdefault(rid, f"in batch of the person's other case {sorted(in_sibling_batch)}")
                continue
            linked = store.records_linked_to(rid, values)
            # (b) identifier matches inside the request window
            if any(r["requested_at"] <= x["captured_at"] <= r["audit_at"] for x in linked):
                scopes[c].add(rid, "identity", owners)
                continue
            # (c) retained records, attributed to this person's case nearest in time
            if category_of(store, rid) in RETAINED_CATEGORIES and linked:
                first = min(x["captured_at"] for x in linked)
                nearest = min([c] + siblings, key=lambda s: abs((ts(first) - ts(req[s]["audit_at"])).total_seconds()))
                if nearest == c:
                    scopes[c].add(rid, "retained", owners)
                else:
                    scopes[c].excluded.setdefault(rid, f"retained record nearer the person's other case {nearest}")
                continue
            scopes[c].excluded.setdefault(rid, "identifier match only outside the request window")
    return scopes


def scope_summary(scope: CaseScope) -> dict[str, Any]:
    return {
        "case_id": scope.case_id,
        "user_id": scope.user_id,
        "resources": {rid: vars(s) for rid, s in sorted(scope.resources.items())},
        "excluded": dict(sorted(scope.excluded.items())),
        "ambiguous": scope.ambiguous,
    }
