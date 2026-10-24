"""Log evidence extractor.

Handles the six line layouts present in the dataset:

    logfmt     2026-09-26T07:49:50Z level=INFO service=x trace_id=t event=e k=v ...
    bracket    2026-07-28T05:51:18Z [DEBUG] [service] [trace] event: k="v" ...
    pipe       2026-07-03T07:51:32Z | DEBUG | service | trace | event | k="v" ...
    syslog     2026-08-22T10:51:11Z <15>1 host service trace - - k="v" ...
    json       2026-04-19T06:51:13Z {"service": ..., "trace_id": ..., ...}
    cloud      2026-09-30T11:51:03Z requestId=t level=I function=svc message="k=\"v\" ..."

Lines are grouped by trace id. A trace's "started" line usually carries the
lookup identifier while the "completed" line carries the resource id and the
state, so identifiers from every line of a trace are linked to each resource
the trace touched.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .common import collect_ids, make_record, merge_ids

TS = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)"
JSON_LINE = re.compile(rf"^{TS}\s+(\{{.*\}})\s*$")
PIPE_LINE = re.compile(rf"^{TS}\s+\|\s*(\w+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.*)$")
BRACKET_LINE = re.compile(rf"^{TS}\s+\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([\w.-]+):\s*(.*)$")
SYSLOG_LINE = re.compile(rf"^{TS}\s+<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$")
PLAIN_LINE = re.compile(rf"^{TS}\s+(.*)$")
KV = re.compile(r'([\w.]+)=("(?:[^"\\]|\\.)*"|\S+)')

SERVICE_KEYS = ("service", "logger", "function")
TRACE_KEYS = ("trace_id", "requestid", "trace")
META_KEYS = {
    "level", "severity", "logger", "function", "service", "trace_id", "requestid", "trace", "instance",
    "pod", "region", "attempt", "duration_ms", "latency_ms", "took_ms", "shard", "wait_ms", "pool",
    "host", "message", "seq", "retry_in_ms", "cursor", "batch",
}


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def parse_kv(text: str) -> dict[str, str]:
    return {k.lower(): unquote(v) for k, v in KV.findall(text)}


def parse_line(line: str) -> dict[str, Any] | None:
    """Normalize one log line into {ts, service, trace, event, fields}."""
    fields: dict[str, Any]
    if m := JSON_LINE.match(line):
        ts = m.group(1)
        try:
            fields = {k.lower(): v for k, v in json.loads(m.group(2)).items()}
        except json.JSONDecodeError:
            fields = parse_kv(m.group(2))
    elif m := PIPE_LINE.match(line):
        ts, level, service, trace, event, rest = m.groups()
        fields = {"level": level, "service": service, "trace_id": trace, "event": event, **parse_kv(rest)}
    elif m := BRACKET_LINE.match(line):
        ts, level, service, trace, event, rest = m.groups()
        fields = {"level": level, "service": service, "trace_id": trace, "event": event, **parse_kv(rest)}
    elif m := SYSLOG_LINE.match(line):
        ts, host, service, trace, rest = m.groups()
        fields = {"host": host, "service": service, "trace_id": trace, **parse_kv(rest)}
    elif m := PLAIN_LINE.match(line):
        ts, rest = m.groups()
        fields = parse_kv(rest)
        first = rest.split(" ", 1)[0]
        if first.isupper() and "=" not in first:
            fields.setdefault("level", first)
    else:
        return None
    if isinstance(fields.get("message"), str) and "=" in fields["message"]:
        fields = {**fields, **parse_kv(fields["message"])}
    return {
        "ts": ts,
        "service": next((fields[k] for k in SERVICE_KEYS if fields.get(k)), None),
        "trace": next((fields[k] for k in TRACE_KEYS if fields.get(k)), None),
        "event": fields.get("event"),
        "fields": fields,
    }


def content_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k not in META_KEYS}


def extract(file_id: str, captured_at: str, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parsed: list[tuple[int, dict[str, Any]]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        entry = parse_line(line)
        if entry:
            parsed.append((line_no, entry))

    trace_links: dict[str, dict[str, list[str]]] = {}
    trace_events: dict[str, list[str]] = {}
    for _, entry in parsed:
        if entry["trace"]:
            linked, _ = collect_ids(content_fields(entry["fields"]))
            trace_links[entry["trace"]] = merge_ids(trace_links.get(entry["trace"], {}), linked)
            trace_events.setdefault(entry["trace"], []).append(
                f"{entry['event'] or ''} {entry['fields'].get('source', '')}")

    groups: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
    for line_no, entry in parsed:
        resource_id = entry["fields"].get("resource_id")
        if not resource_id:
            continue
        key = (entry["trace"] or f"line{line_no}", resource_id)
        group = groups.setdefault(key, {"lines": [], "fields": {}, "events": [], "services": [], "ts": None})
        group["lines"].append(line_no)
        group["fields"].update(content_fields(entry["fields"]))
        group["events"].append(entry["event"] or "")
        if entry["service"]:
            group["services"].append(entry["service"])
        group["ts"] = entry["ts"]

    records = []
    for (trace, resource_id), group in groups.items():
        fields = group["fields"]
        lines = group["lines"]
        records.append(make_record(
            file_id=file_id, captured_at=captured_at, source_type="log", resource_id=resource_id,
            fields=fields, extractor="log",
            locator=f"line {lines[0]}" if len(lines) == 1 else f"lines {lines[0]}-{lines[-1]}",
            observed_system=fields.get("system") or (group["services"][-1] if group["services"] else None),
            explicit_category=fields.get("record_type"),
            context=group["events"] + trace_events.get(trace, []) + group["services"],
            extra_link_ids=trace_links.get(trace), observed_at=group["ts"],
        ))
    return records, []
