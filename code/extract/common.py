"""Shared record model and normalization helpers for evidence extraction.

Every extractor emits plain dicts built by `make_record`, so the downstream
auditor sees one schema regardless of the source format:

    {file_id, captured_at, source_type, system_id, resource_id, category, state,
     identifiers, exposed, aliases, copy, copy_role, observed_at, attributes,
     locator, extractor}

`identifiers` holds every identifier that links the record to a person (lookup
keys included); `exposed` holds only identifiers that sit in the record's own
content fields, which is what redaction/anonymization policy cares about.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

EXTRACTOR_VERSION = "1"

ID_KINDS = ("emails", "phones", "names", "hashes", "user_ids")

STATES = ("PRESENT", "ABSENT", "REDACTED", "ANONYMIZED", "ACTIVE", "DISABLED", "BLOCKED", "UNKNOWN")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_ID_RE = re.compile(r"\buser_\d+\b")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone"
    r"|cache|email_delivery|marketing|legal_hold|object)_\d+\b"
)
# Business references that other evidence maps to resource ids
# (e.g. ORD-20264743 <-> legal_hold_4743, NST-2026-0718-1842 <-> export_1842).
ALIAS_RE = re.compile(r"\b(?:ORD-\d{6,}|NST-\d{4}-\d{4}-\d+|CS-\d+|CJX-\d{8}-\d{4}|FIN-\d+)\b")

# Only these categories have a flow/restore state; elsewhere "active" just means the record exists.
FLOW_CATEGORIES = {"DATA_FLOW", "BACKUP_ARCHIVE"}

# Resource prefix -> (logical owning system, default data category).
PREFIXES = {
    "account": ("account_service", "USER_PROFILE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support": ("support_platform", "SUPPORT_MESSAGE"),
    "export": ("export_service", "USER_EXPORT"),
    "data_flow": ("integration_service", "DATA_FLOW"),
    "backup": ("backup_service", "BACKUP_ARCHIVE"),
    "audit_record": ("security_service", "SECURITY_AUDIT_LOG"),
    "tombstone": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "cache": ("cache_service", "CACHE_ENTRY"),
    "email_delivery": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE"),
    "legal_hold": ("legal_service", "LEGAL_HOLD_RECORD"),
    "object": ("object_storage", "SUPPORT_ATTACHMENT"),
}

CATEGORIES = {
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
    "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
    "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE", "DATA_FLOW",
}

# Keys whose values identify who was looked up, not what the record contains.
LINK_KEYS = {
    "lookup_key", "subject_key", "match_key", "scan_key", "correlation_key", "query",
    "subject_selector", "subject_ref", "lookup_value", "subject_reference",
}
NAME_KEYS = {"full_name", "name", "display_name", "requester_name", "customer_name", "owner_name", "customer"}
# Values under these keys look like identifiers but are not personal data.
IGNORE_KEYS = {"integrity_seal", "checksum", "archive_key", "object_key", "archive_path", "trace_id",
               "requestid", "pod", "instance", "region", "index", "index_name"}

EXPLICIT_STATE = {
    "FOUND": "PRESENT", "PRESENT": "PRESENT", "IDENTIFIABLE": "PRESENT", "HIT": "PRESENT",
    "NOT_FOUND": "ABSENT", "ABSENT": "ABSENT", "MISS": "ABSENT", "DELETED": "ABSENT",
    "ANONYMIZED": "ANONYMIZED", "REDACTED": "REDACTED",
    "ACTIVE": "ACTIVE", "ENABLED": "ACTIVE",
    "DISABLED": "DISABLED", "STOPPED": "DISABLED",
    "BLOCKED": "BLOCKED",
}

TRUE_VALUES = {"true", "1", "yes", "t"}
FALSE_VALUES = {"false", "0", "no", "f"}


def empty_ids() -> dict[str, list[str]]:
    return {k: [] for k in ID_KINDS}


def resource_prefix(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    for prefix in sorted(PREFIXES, key=len, reverse=True):
        if resource_id.startswith(prefix + "_") and resource_id[len(prefix) + 1:].isdigit():
            return prefix
    return None


def system_for(resource_id: str | None, observed: str | None = None) -> str | None:
    prefix = resource_prefix(resource_id)
    return PREFIXES[prefix][0] if prefix else observed


def is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in {"", "null", "none"})


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def add_id(into: dict[str, list[str]], kind: str, value: str) -> None:
    value = value.strip()
    if kind in ("emails", "hashes"):
        value = value.lower()
    if kind == "phones":
        value = re.sub(r"\s+", " ", value)
    if value and value not in into[kind]:
        into[kind].append(value)


def scan_text(text: str, into: dict[str, list[str]]) -> None:
    """Add every identifier pattern found in free text."""
    for m in EMAIL_RE.findall(text):
        add_id(into, "emails", m)
    for m in HASH_RE.findall(text):
        add_id(into, "hashes", m)
    for m in USER_ID_RE.findall(text):
        add_id(into, "user_ids", m)
    for m in PHONE_RE.findall(text):
        add_id(into, "phones", m)


def collect_ids(fields: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (all identifiers, exposed identifiers) for a flat field dict."""
    linked, exposed = empty_ids(), empty_ids()
    for key, value in fields.items():
        if is_null(value) or isinstance(value, bool):
            continue
        leaf = re.sub(r"\[\d+\]$", "", key.rsplit(".", 1)[-1].lower())
        if leaf in IGNORE_KEYS:
            continue
        text = str(value)
        targets = [linked] if leaf in LINK_KEYS else [linked, exposed]
        for target in targets:
            scan_text(text, target)
            if leaf in NAME_KEYS and not EMAIL_RE.search(text) and not RESOURCE_RE.fullmatch(text):
                add_id(target, "names", text)
    return linked, exposed


def merge_ids(*groups: dict[str, list[str]]) -> dict[str, list[str]]:
    out = empty_ids()
    for group in groups:
        for kind in ID_KINDS:
            for value in group.get(kind, []):
                add_id(out, kind, value)
    return out


def direct_exposure(exposed: dict[str, list[str]], include_refs: bool) -> bool:
    kinds = ["emails", "phones", "names", "hashes"] + (["user_ids"] if include_refs else [])
    return any(exposed.get(k) for k in kinds)


def infer_category(resource_id: str | None, explicit: str | None, context: Iterable[str] = ()) -> str | None:
    """Category from an explicit record_type, else from context words, else from the prefix."""
    if explicit and explicit.upper() in CATEGORIES:
        return explicit.upper()
    words = " ".join(c for c in context if c).lower()
    prefix = resource_prefix(resource_id)
    if prefix == "support":
        if "attachment" in words:
            return "SUPPORT_ATTACHMENT"
        if "contact_index" in words or "contact_query" in words:
            return "SECONDARY_IDENTIFIER"
    if prefix == "analytics":
        if "email_opt_in" in words or "contact_lookup" in words:
            return "MARKETING_PROFILE"
        if "telemetry" in words:
            return "PRODUCT_TELEMETRY"
    if prefix:
        return PREFIXES[prefix][1]
    return None


def derive_state(category: str | None, fields: dict[str, Any], exposed: dict[str, list[str]],
                 explicit: str | None = None) -> str:
    """Observed state of one record, from explicit status words first, then field signals."""
    lowered = {k.rsplit(".", 1)[-1].lower(): v for k, v in fields.items()}

    def scoped(state: str) -> str:
        if state in ("ACTIVE", "DISABLED", "BLOCKED") and category not in FLOW_CATEGORIES:
            return "PRESENT"
        return state

    if explicit:
        mapped = EXPLICIT_STATE.get(str(explicit).strip().upper())
        if mapped:
            return scoped(mapped)

    result = str(lowered.get("result", "")).strip().upper()
    if result in EXPLICIT_STATE:
        return scoped(EXPLICIT_STATE[result])

    status = as_int(lowered.get("http_status", lowered.get("status_code")))
    if status == 404:
        return "ABSENT"
    if as_int(lowered.get("rows")) == 0 or as_int(lowered.get("total_hits", lowered.get("total"))) == 0:
        return "ABSENT"

    if category == "DATA_FLOW":
        enabled = as_bool(lowered.get("enabled"))
        if enabled is not None:
            return "ACTIVE" if enabled else "DISABLED"
        delivery = str(lowered.get("last_delivery_status", "")).lower()
        if delivery == "stopped":
            return "DISABLED"
        if delivery == "delivered":
            return "ACTIVE"

    if category == "BACKUP_ARCHIVE":
        decision = str(lowered.get("decision", "")).strip().lower()
        if decision in ("deny", "denied", "block", "blocked"):
            return "BLOCKED"
        if decision in ("allow", "allowed", "approve", "approved"):
            return "ACTIVE"
        allowed = as_bool(lowered.get("restore_allowed"))
        if allowed is not None:
            return "ACTIVE" if allowed else "BLOCKED"
        block = as_bool(lowered.get("deletion_block"))
        if block is True:
            return "BLOCKED"
        if as_bool(lowered.get("eligible")) is True and block is False:
            return "ACTIVE"

    # Scanners report a count of direct identifiers; trust it over field-by-field guessing.
    reported = as_int(lowered.get("direct_identifiers"))
    if category in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"):
        exposed_now = reported > 0 if reported is not None else direct_exposure(exposed, include_refs=True)
        return "PRESENT" if exposed_now else "ANONYMIZED"
    if category in ("SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE"):
        exposed_now = reported > 0 if reported is not None else direct_exposure(exposed, include_refs=False)
        return "PRESENT" if exposed_now else "REDACTED"

    if "profile" in lowered and is_null(lowered.get("profile")) and status not in (None, 200):
        return "ABSENT"
    return "PRESENT"


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts/lists into dotted scalar keys."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        if all(not isinstance(v, (dict, list)) for v in obj):
            out[prefix] = ", ".join("" if v is None else str(v) for v in obj)
        else:
            for i, value in enumerate(obj):
                out.update(flatten(value, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def make_record(*, file_id: str, captured_at: str, source_type: str, resource_id: str | None,
                fields: dict[str, Any], extractor: str, locator: str,
                observed_system: str | None = None, explicit_category: str | None = None,
                explicit_state: str | None = None, context: Iterable[str] = (),
                extra_link_ids: dict[str, list[str]] | None = None,
                copy: str | None = None, copy_role: str | None = None,
                observed_at: str | None = None, text: str = "") -> dict[str, Any]:
    linked, exposed = collect_ids(fields)
    if extra_link_ids:
        linked = merge_ids(linked, extra_link_ids)
    joined = " ".join(str(v) for v in fields.values() if v is not None) + " " + text
    category = infer_category(resource_id, explicit_category, list(context) + list(fields))
    return {
        "file_id": file_id,
        "captured_at": captured_at,
        "source_type": source_type,
        "system_id": system_for(resource_id, observed_system),
        "resource_id": resource_id,
        "category": category,
        "state": derive_state(category, fields, exposed, explicit_state),
        "identifiers": linked,
        "exposed": exposed,
        "aliases": sorted(set(ALIAS_RE.findall(joined))),
        "linked_resources": [],
        "copy": copy,
        "copy_role": copy_role,
        "observed_at": observed_at,
        "attributes": {k: v for k, v in fields.items() if not is_null(v)},
        "locator": locator,
        "extractor": f"{extractor}@{EXTRACTOR_VERSION}",
    }
