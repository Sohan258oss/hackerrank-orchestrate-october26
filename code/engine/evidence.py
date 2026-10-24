"""Evidence store for the auditor: extracted records, snapshot coverage, and activity.

Responsibilities:
  * index normalized evidence records by resource;
  * attribute each resource to people: any identifier in any of its records
    (or a LINK_RESOURCE_IDENTITY activity) that belongs to a user makes that
    user an owner. Ownership answers "whose record is this", never "what state
    is it in", so it may use evidence from any capture time;
  * resolve records that print no resource id (screenshots, PDFs) through
    business aliases: order / statement / ticket numbers and file names that
    other evidence or link activities tie to a resource id.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from extract.common import PREFIXES, RESOURCE_RE, resource_prefix
from extract.env import CACHE_ROOT

from .identity import IdentityIndex, email_sha256, phone_digits

LINK_ROW = re.compile(
    r"(?:linked|confirmed)\s+(?P<rid>\S+)\s+\((?P<alias>[^)]+)\)\s+(?:to|belongs only to)\s+(?P<email>\S+@\S+?)\s+and\s+(?P<uid>user_\d+)"
)
FILE_ALIAS = re.compile(r"[\w.-]+\.(?:pdf|zip|jpg|jpeg|png|csv|tar\.zst)$", re.I)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def link_values(record: dict[str, Any]) -> set[str]:
    ids = record["identifiers"]
    values = {e.lower() for e in ids["emails"]} | {h.lower() for h in ids["hashes"]} | set(ids["user_ids"])
    values |= {email_sha256(e) for e in ids["emails"]}
    values |= {phone_digits(p) for p in ids["phones"] if phone_digits(p)}
    return values


class EvidenceStore:
    def __init__(self, dataset: Path, index: IdentityIndex, extract_dir: Path | None = None) -> None:
        extract_dir = extract_dir or CACHE_ROOT / "extract"
        self.index = index
        with (dataset / "evidence_files.csv").open(encoding="utf-8", newline="") as fh:
            self.manifest = {r["file_id"]: r for r in csv.DictReader(fh)}
        with (dataset / "system_activity.csv").open(encoding="utf-8", newline="") as fh:
            self.activity = sorted(csv.DictReader(fh), key=lambda a: (a["event_time"], a["activity_id"]))
        self.records = [json.loads(line) for line in (extract_dir / "records.jsonl").open(encoding="utf-8")]
        self.coverage = [json.loads(line) for line in (extract_dir / "coverage.jsonl").open(encoding="utf-8")]

        self.link_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.alias_to_resource: dict[str, set[str]] = defaultdict(set)
        for a in self.activity:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY" and (m := LINK_ROW.search(a["details"])):
                row = {**m.groupdict(), "activity_id": a["activity_id"], "event_time": a["event_time"]}
                self.link_rows[row["rid"]].append(row)
                if row["alias"] != row["rid"]:
                    self.alias_to_resource[row["alias"].lower()].add(row["rid"])
        for r in self.records:
            if r["resource_id"]:
                for alias in self.aliases_of(r):
                    self.alias_to_resource[alias].add(r["resource_id"])
        self.alias_resolved = 0
        for r in self.records:
            if not r["resource_id"]:
                self.resolve_by_alias(r)

        self.by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.records:
            if r["resource_id"]:
                self.by_resource[r["resource_id"]].append(r)
        self.activity_by_resource: dict[str, list[dict[str, str]]] = defaultdict(list)
        for a in self.activity:
            self.activity_by_resource[a["resource_id"]].append(a)
            for rid in set(RESOURCE_RE.findall(a["details"])) - {a["resource_id"]}:
                self.activity_by_resource[rid].append(a)

        self._owners: dict[str, set[str]] = {}
        self.resources_by_owner: dict[str, set[str]] = defaultdict(set)
        for rid in set(self.by_resource) | set(self.link_rows):
            for uid in self.owners(rid):
                self.resources_by_owner[uid].add(rid)

    # ------------------------------------------------------------- aliases
    @staticmethod
    def aliases_of(record: dict[str, Any]) -> set[str]:
        out = {a.lower() for a in record.get("aliases", [])}
        for key, value in record["attributes"].items():
            text = str(value)
            if key.rsplit(".", 1)[-1] in ("file_name", "object_key", "archive_key", "file") or FILE_ALIAS.search(text):
                out.add(text.rsplit("/", 1)[-1].lower())
        return {a for a in out if a and not RESOURCE_RE.fullmatch(a)}

    def resolve_by_alias(self, record: dict[str, Any]) -> None:
        """Give a resource-less record the resource id its aliases (or a linked user reference) point to."""
        keys = self.aliases_of(record) | {u.lower() for u in record["identifiers"]["user_ids"]}
        hits: set[str] = set()
        for key in keys:
            hits |= self.alias_to_resource.get(key, set())
        if len(hits) == 1:
            rid = hits.pop()
            prefix = resource_prefix(rid)
            record["resource_id"] = rid
            record["system_id"] = PREFIXES[prefix][0] if prefix else record["system_id"]
            record["category"] = record["category"] or (PREFIXES[prefix][1] if prefix else None)
            record["resolved_via_alias"] = True
            self.alias_resolved += 1

    # ------------------------------------------------------------- ownership
    def owners(self, rid: str) -> set[str]:
        if rid not in self._owners:
            owners: set[str] = set()
            for r in self.by_resource.get(rid, []):
                owners |= self.index.owners(link_values(r))
            for row in self.link_rows.get(rid, []):
                owners |= self.index.owners({row["email"].lower(), row["uid"]})
            self._owners[rid] = owners
        return self._owners[rid]

    def records_linked_to(self, rid: str, user_values: set[str]) -> list[dict[str, Any]]:
        return [r for r in self.by_resource.get(rid, []) if link_values(r) & user_values]

    # ------------------------------------------------------------- activity
    def deletion_batch(self, start: datetime, end: datetime) -> list[dict[str, str]]:
        return [a for a in self.activity
                if a["source_system"] == "deletion_worker" and start <= ts(a["event_time"]) <= end]

    def activities_for(self, rid: str, until: str | None = None) -> list[dict[str, str]]:
        return [a for a in self.activity_by_resource.get(rid, []) if until is None or a["event_time"] <= until]
