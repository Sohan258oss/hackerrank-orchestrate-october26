"""Shared setup for the auditor: stages 1-4 over sample and evaluation requests together.

Scoping needs every request at once because deletion batches of different
cases interleave, so the pipeline always loads both request files and callers
pick the cases they report on.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .evidence import EvidenceStore
from .identity import Identity, IdentityIndex, Resolution
from .intent import IntentResult, classify_all
from .scope import CaseScope, build_scopes
from .state import ResourceState, case_state

REQUEST_FILES = {"sample": "sample_deletion_requests.csv", "eval": "deletion_requests.csv"}


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def case_sort_key(case_id: str) -> tuple[str, int]:
    prefix, _, number = case_id.rpartition("_")
    return (prefix, int(number)) if number.isdigit() else (case_id, 0)


@dataclass
class Pipeline:
    requests: dict[str, list[dict[str, str]]]          # set name -> request rows
    by_case: dict[str, dict[str, str]]
    intents: dict[str, IntentResult]
    resolutions: dict[str, Resolution]
    identities: dict[str, Identity]                     # deletion cases with a resolved person
    store: EvidenceStore
    scopes: dict[str, CaseScope]

    def state(self, case_id: str) -> tuple[dict[str, ResourceState], list[dict[str, str]]]:
        if case_id not in self.scopes:
            return {}, []
        return case_state(self.store, self.scopes[case_id], self.by_case[case_id]["audit_at"])


def prepare(dataset: Path, use_llm: bool = True) -> Pipeline:
    requests = {name: load_csv(dataset / f) for name, f in REQUEST_FILES.items()}
    combined = requests["sample"] + requests["eval"]
    by_case = {r["case_id"]: r for r in combined}
    intents = classify_all({r["case_id"]: r["request_text"] for r in combined}, use_llm=use_llm)
    index = IdentityIndex(dataset)
    resolutions = {r["case_id"]: index.resolve(r["email"], as_of=r["audit_at"]) for r in combined}
    identities = {c: res.identity for c, res in resolutions.items()
                  if res.identity and intents[c].authorizes_deletion is not False}
    store = EvidenceStore(dataset, index)
    scopes = build_scopes(combined, identities, store)
    return Pipeline(requests, by_case, intents, resolutions, identities, store, scopes)
