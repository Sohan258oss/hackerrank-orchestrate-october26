"""JSON evidence extractor.

The JSON files come in several shapes (snapshot bundles, per-service resource
exports, flat manifests/ledgers, single-collection reports). Rather than one
parser per shape, the walker treats any object carrying a `resource_id` as a
record and inherits context (data store, collection, service, capture time)
from its ancestors. Nested objects repeating the same `resource_id` (search
hits inside a query) are folded into their parent record.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import PREFIXES, flatten, make_record

KNOWN_SYSTEMS = {system for system, _ in PREFIXES.values()}
CONTEXT_KEYS = ("data_store", "collection", "source", "origin")
TIME_KEYS = ("observed_at", "recorded_at", "captured_at")


def split_children(node: Any, resource_id: str) -> tuple[Any, list[Any]]:
    """Return node without nested records of *other* resources, plus those nested records."""
    if isinstance(node, dict):
        if "resource_id" in node and node["resource_id"] != resource_id:
            return None, [node]
        clean, found = {}, []
        for key, value in node.items():
            sub, kids = split_children(value, resource_id)
            found += kids
            if sub is not None:
                clean[key] = sub
        return clean, found
    if isinstance(node, list):
        clean_list, found = [], []
        for value in node:
            sub, kids = split_children(value, resource_id)
            found += kids
            if sub is not None:
                clean_list.append(sub)
        return clean_list, found
    return node, []


class Walker:
    def __init__(self, file_id: str, captured_at: str) -> None:
        self.file_id = file_id
        self.captured_at = captured_at
        self.records: list[dict[str, Any]] = []
        self.coverage: list[dict[str, Any]] = []

    def walk(self, node: Any, ctx: dict[str, Any], path: str) -> None:
        if isinstance(node, list):
            for i, value in enumerate(node):
                self.walk(value, ctx, f"{path}[{i}]")
            return
        if not isinstance(node, dict):
            return
        if "resource_id" in node:
            self.emit(node, ctx, path)
            return
        local = dict(ctx)
        for key in CONTEXT_KEYS:
            if isinstance(node.get(key), str):
                local[key] = node[key]
        if isinstance(node.get("captured_at"), str):
            local["snapshot_at"] = node["captured_at"]
        if "data_store" in node and isinstance(node.get("records"), list):
            self.add_snapshot_coverage(node, path)
        for key, value in node.items():
            child = dict(local)
            if key in KNOWN_SYSTEMS:
                child["service"] = key
            if isinstance(value, (dict, list)):
                self.walk(value, child, f"{path}.{key}" if path else key)

    def emit(self, node: dict[str, Any], ctx: dict[str, Any], path: str) -> None:
        resource_id = node["resource_id"]
        clean, children = split_children(node, resource_id)
        fields = flatten(clean)
        observed_system = node.get("system") or ctx.get("service") or ctx.get("data_store")
        observed_at = next((str(node[k]) for k in TIME_KEYS if node.get(k)), ctx.get("snapshot_at"))
        copy = "/".join(x for x in (ctx.get("data_store") or ctx.get("service"), ctx.get("collection")) if x) or None
        self.records.append(make_record(
            file_id=self.file_id, captured_at=self.captured_at, source_type="json",
            resource_id=resource_id, fields=fields, extractor="json", locator=path,
            observed_system=observed_system, explicit_category=node.get("record_type"),
            context=[ctx.get("collection") or "", ctx.get("data_store") or "", path],
            copy=copy, copy_role="snapshot" if ctx.get("data_store") else "export",
            observed_at=observed_at,
        ))
        for i, child in enumerate(children):
            self.walk(child, ctx, f"{path}<{i}>")

    def add_snapshot_coverage(self, snapshot: dict[str, Any], path: str) -> None:
        ids = sorted(r["resource_id"] for r in snapshot["records"] if isinstance(r, dict) and r.get("resource_id"))
        self.coverage.append({
            "file_id": self.file_id, "captured_at": self.captured_at, "source_type": "json",
            "copy": f"{snapshot['data_store']}/{snapshot.get('collection', '')}", "copy_role": "snapshot",
            "snapshot_at": snapshot.get("captured_at"), "row_count": len(ids), "resource_ids": ids,
            "system_ids": [snapshot["data_store"]], "scope_systems": [], "locator": path,
        })


def extract(file_id: str, captured_at: str, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    walker = Walker(file_id, captured_at)
    walker.walk(json.loads(path.read_text(encoding="utf-8")), {}, "")
    return walker.records, walker.coverage
