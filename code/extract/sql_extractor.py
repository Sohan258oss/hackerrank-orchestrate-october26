"""SQL snapshot extractor.

Each .sql evidence file is executed into a private in-memory SQLite database.
Every table is kept as its own copy (`copy` = table name, `copy_role` =
primary / replica / inventory), so a row that survives only in a replica stays
visible as replica drift instead of being merged away.

Besides records, the extractor reports coverage: which systems each table
captures and which resource ids it holds, so an empty or row-less table can
later serve as proof of absence.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .common import flatten, make_record

# Plain (non-JSON) table families -> owning system.
TABLE_SYSTEMS = [
    (re.compile(r"customer_profiles"), "account_service"),
    (re.compile(r"(customer_)?search_documents"), "search_service"),
    (re.compile(r"customer_analytics_profiles"), "analytics_platform"),
    (re.compile(r"support_records"), "support_platform"),
    (re.compile(r"customer_exports"), "export_service"),
    (re.compile(r"connector_subscriptions"), "integration_service"),
    (re.compile(r"backup_restore_entries"), "backup_service"),
    (re.compile(r"security_audit_records"), "security_service"),
]
JSON_COLUMNS = ("record_json", "payload", "record")
SYSTEM_COLUMNS = ("origin_system", "data_store", "system")
COLLECTION_COLUMNS = ("collection_name", "collection")
TIME_COLUMNS = ("updated_at", "indexed_at", "observed_at", "recorded_at")


def copy_role(table: str) -> str:
    if "primary" in table:
        return "primary"
    if "replica" in table:
        return "replica"
    return "inventory"


def table_system(table: str) -> str | None:
    for pattern, system in TABLE_SYSTEMS:
        if pattern.search(table):
            return system
    return None


def load_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(path.read_text(encoding="utf-8"))
    return conn


def extract(file_id: str, captured_at: str, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conn = load_database(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        scope: list[str] = []
        if "snapshot_scope" in tables:
            scope = sorted(r[0] for r in conn.execute("SELECT origin_system FROM snapshot_scope"))

        records: list[dict[str, Any]] = []
        coverage: list[dict[str, Any]] = []
        generic_systems: set[str] = set()
        generic_tables: list[dict[str, Any]] = []

        for table in tables:
            if table == "snapshot_scope":
                continue
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            json_col = next((c for c in JSON_COLUMNS if c in columns), None)
            role = copy_role(table)
            resource_ids: list[str] = []
            row_systems: set[str] = set()

            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                row_dict = dict(row)
                fields = {k: v for k, v in row_dict.items() if k != json_col}
                observed_system = next((row_dict[c] for c in SYSTEM_COLUMNS if row_dict.get(c)), None)
                collection = next((row_dict[c] for c in COLLECTION_COLUMNS if row_dict.get(c)), None)
                if json_col and row_dict.get(json_col):
                    try:
                        fields.update(flatten(json.loads(row_dict[json_col])))
                    except json.JSONDecodeError:
                        fields[json_col] = row_dict[json_col]
                resource_id = row_dict.get("resource_id") or fields.get("resource_id")
                observed_at = next((str(fields[c]) for c in TIME_COLUMNS if fields.get(c)), None)
                record = make_record(
                    file_id=file_id, captured_at=captured_at, source_type="sql",
                    resource_id=resource_id, fields=fields, extractor="sql",
                    locator=f"{table}:{resource_id}", observed_system=observed_system or table_system(table),
                    context=[table, collection or ""], copy=table, copy_role=role,
                    observed_at=observed_at,
                )
                records.append(record)
                if resource_id:
                    resource_ids.append(resource_id)
                if record["system_id"]:
                    row_systems.add(record["system_id"])

            entry: dict[str, Any] = {
                "file_id": file_id, "captured_at": captured_at, "source_type": "sql",
                "copy": table, "copy_role": role, "row_count": len(resource_ids),
                "resource_ids": sorted(resource_ids), "scope_systems": scope,
            }
            if json_col:
                # Multi-store tables: the file as a whole defines which systems were captured.
                generic_systems |= row_systems
                generic_tables.append(entry)
            else:
                fixed = table_system(table)
                systems = {fixed} if fixed else row_systems
                if scope and fixed and fixed not in scope:
                    systems = set()
                entry["system_ids"] = sorted(s for s in systems if s)
            coverage.append(entry)

        for entry in generic_tables:
            entry["system_ids"] = sorted(generic_systems)
        return records, coverage
    finally:
        conn.close()
