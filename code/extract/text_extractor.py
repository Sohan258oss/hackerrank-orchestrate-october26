"""Plain-text evidence extractor.

Primary format is the shift-handover list:

    - 2026-10-01T12:00:00Z | search_service | search_052 | SEARCH_INDEX_ENTRY | FOUND | someone@example.com

Any other line that mentions a resource id becomes a record with state
UNKNOWN, so nothing in a text file is silently dropped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import RESOURCE_RE, make_record

HANDOVER_ROW = re.compile(
    r"^\s*-?\s*(\d{4}-\d{2}-\d{2}T[\d:]+Z)\s*\|\s*([\w.-]+)\s*\|\s*([\w.-]+)\s*\|\s*([A-Z_]+)\s*\|\s*([A-Z_]+)\s*\|\s*(.*?)\s*$"
)
# For these states the identifier column is content the record still holds; otherwise it is the lookup key.
CONTENT_STATES = {"FOUND", "IDENTIFIABLE", "ACTIVE", "PRESENT"}


def extract(file_id: str, captured_at: str, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if m := HANDOVER_ROW.match(line):
            ts, system, resource_id, category, state, identifier = m.groups()
            key = "observed_identifier" if state in CONTENT_STATES else "subject_reference"
            records.append(make_record(
                file_id=file_id, captured_at=captured_at, source_type="text", resource_id=resource_id,
                fields={"system": system, "record_type": category, "result": state, key: identifier},
                extractor="text", locator=f"line {line_no}", observed_system=system,
                explicit_category=category, explicit_state=state, observed_at=ts,
            ))
            continue
        for resource_id in dict.fromkeys(RESOURCE_RE.findall(line)):
            record = make_record(
                file_id=file_id, captured_at=captured_at, source_type="text", resource_id=resource_id,
                fields={"text": line.strip()}, extractor="text", locator=f"line {line_no}",
            )
            record["state"] = "UNKNOWN"
            records.append(record)
    return records, []
