"""Screenshot extractor: vision LLM with a strict-JSON prompt, cached by file hash.

The model transcribes visible records; normalization into the shared record
schema (system, category, state, exposed identifiers) happens here in code,
so the LLM only reads the screen and never applies policy.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import cache, llm
from .common import EXTRACTOR_VERSION, RESOURCE_RE, STATES, USER_ID_RE, make_record

PROMPT = "image_extraction"
LIST_FIELDS = ("emails", "phones", "names", "user_ids", "hashes", "aliases")
# Prefix each transcribed identifier with a key that common.collect_ids treats as record content.
ID_FIELD_KEYS = {"emails": "email", "phones": "phone", "names": "name", "user_ids": "user_id", "hashes": "email_hash"}
# Known misreads of business references, e.g. "ORDER-20265034" for the printed "ORD-20265034".
ALIAS_REPAIRS = [(re.compile(r"^ORDER-(\d{8})$"), r"ORD-\1")]


def repair_alias(value: str) -> str:
    for pattern, replacement in ALIAS_REPAIRS:
        value = pattern.sub(replacement, value)
    return value


def resource_tokens(rec: dict[str, Any]) -> list[str]:
    """Every resource id the model wrote anywhere in a record, in order of appearance."""
    texts = [rec["record_kind"], rec["status_text"] or ""] + rec["aliases"]
    texts += [f"{f['label']} {f['value']}" for f in rec["fields"]]
    return list(dict.fromkeys(m for text in texts for m in RESOURCE_RE.findall(text)))


def validate(payload: Any) -> dict[str, Any]:
    """Coerce model output into the documented schema or raise ValueError."""
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("expected an object with a 'records' array")
    records = []
    for raw in payload["records"]:
        if not isinstance(raw, dict):
            raise ValueError("each record must be an object")
        rec: dict[str, Any] = {}
        rid = raw.get("resource_id")
        rec["resource_id"] = rid.strip() if isinstance(rid, str) and RESOURCE_RE.fullmatch(rid.strip()) else None
        rec["record_kind"] = str(raw.get("record_kind") or "")
        rec["status_text"] = raw.get("status_text") if isinstance(raw.get("status_text"), str) else None
        state = str(raw.get("observed_state") or "UNKNOWN").upper()
        rec["observed_state"] = state if state in STATES else "UNKNOWN"
        for key in LIST_FIELDS:
            values = raw.get(key) or []
            if isinstance(values, str):
                values = [values]
            rec[key] = [str(v).strip() for v in values if isinstance(v, (str, int)) and str(v).strip()]
        if isinstance(rid, str) and rid.strip() and rec["resource_id"] is None:
            rec["aliases"].append(rid.strip())
        rec["fields"] = [
            {"label": str(item["label"]), "value": str(item["value"])}
            for item in raw.get("fields") or []
            if isinstance(item, dict) and item.get("label") is not None and item.get("value") is not None
        ]
        # Models sometimes put the record's own id in a field ("Analytics ID: analytics_7438")
        # and mention related records ("Support: CS-8538 · support_9538"); keep both.
        tokens = resource_tokens(rec)
        if rec["resource_id"] is None and tokens:
            rec["resource_id"] = tokens[0]
        rec["linked_resources"] = [t for t in tokens if t != rec["resource_id"]]
        for value in list(rec["user_ids"]):
            if not USER_ID_RE.fullmatch(value):
                rec["user_ids"].remove(value)
        aliases = [repair_alias(a) for a in rec["aliases"]]
        rec["user_ids"] += [a for a in aliases if USER_ID_RE.fullmatch(a) and a not in rec["user_ids"]]
        rec["aliases"] = list(dict.fromkeys(
            a for a in aliases if not USER_ID_RE.fullmatch(a) and not RESOURCE_RE.fullmatch(a)))
        for item in rec["fields"]:
            item["value"] = " ".join(repair_alias(part) for part in item["value"].split(" "))
        records.append(rec)
    return {
        "screen_title": str(payload.get("screen_title") or ""),
        "product_area": str(payload.get("product_area") or ""),
        "records": records,
    }


def payload_to_records(payload: dict[str, Any], *, file_id: str, captured_at: str, source_type: str,
                       extractor: str, locator_prefix: str, context: list[str]) -> list[dict[str, Any]]:
    out = []
    for i, rec in enumerate(payload["records"]):
        fields: dict[str, Any] = {
            "screen_title": payload["screen_title"],
            "product_area": payload["product_area"],
            "record_kind": rec["record_kind"],
            "status_text": rec["status_text"],
        }
        for kind, key in ID_FIELD_KEYS.items():
            for j, value in enumerate(rec[kind]):
                fields[f"{key}[{j}]"] = value
        for item in rec["fields"]:
            label = re.sub(r"\W+", "_", item["label"].strip().lower()).strip("_") or "field"
            fields[f"field.{label}"] = item["value"]
        record = make_record(
            file_id=file_id, captured_at=captured_at, source_type=source_type,
            resource_id=rec["resource_id"], fields=fields, extractor=extractor,
            locator=f"{locator_prefix}records[{i}]",
            explicit_state=None if rec["observed_state"] == "UNKNOWN" else rec["observed_state"],
            context=context + [rec["record_kind"], payload["screen_title"], payload["product_area"]],
            text=" ".join(rec["aliases"]),
        )
        record["aliases"] = sorted(set(record["aliases"]) | set(rec["aliases"]))
        record["linked_resources"] = rec.get("linked_resources", [])
        out.append(record)
    return out


def run_vision(client: llm.VisionClient, data: bytes, mime: str, *, file_hash: str, file_id: str,
               prompt_name: str, part: str = "") -> dict[str, Any]:
    """One cached vision call; the cache key covers file bytes, page, model and prompt text."""
    key = cache.cache_key(file_hash, prompt_name, part, EXTRACTOR_VERSION, client.identity(),
                          llm.prompt_hash(prompt_name))
    cached = cache.load(key)
    if cached is not None:
        llm.log_cache_hit(file_id, prompt_name, client.provider, client.model)
        return validate(cached)  # re-normalize: validation fixes apply without a new model call
    payload = client.extract_json(data, mime, prompt_name, file_id, validate)
    cache.store(key, payload)
    return payload


def extract(file_id: str, captured_at: str, path: Path,
            client: llm.VisionClient | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    client = client or llm.VisionClient()
    data, mime = llm.image_bytes_from_path(path)
    payload = run_vision(client, data, mime, file_hash=cache.sha256_bytes(data), file_id=file_id,
                         prompt_name=PROMPT)
    return payload_to_records(payload, file_id=file_id, captured_at=captured_at, source_type="image",
                              extractor="image", locator_prefix="", context=[path.stem]), []
