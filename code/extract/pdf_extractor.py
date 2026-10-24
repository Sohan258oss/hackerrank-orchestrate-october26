"""PDF extractor: pdfplumber text first, vision LLM only for pages without a text layer.

Text documents are parsed deterministically:
  * register/table rows ("... | ... | ...") become one record each;
  * every resource id printed outside tables becomes a record carrying the
    document's identifiers;
  * when no resource id is printed outside tables, one document-level record
    (resource_id null) keeps the document's identifiers and business aliases
    (order / statement / ticket numbers) so the auditor can resolve it later.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pdfplumber

from . import cache, llm
from .common import RESOURCE_RE, make_record
from .image_extractor import payload_to_records, run_vision

PROMPT = "pdf_page_extraction"
MIN_TEXT_CHARS = 40
RENDER_DPI = 150

DOC_CATEGORIES = [
    (re.compile(r"register|legal hold|disputed orders|tax record", re.I), "LEGAL_HOLD_RECORD"),
    (re.compile(r"compliance journal|audit", re.I), "SECURITY_AUDIT_LOG"),
    (re.compile(r"attachment|uploader customer|upload", re.I), "SUPPORT_ATTACHMENT"),
    (re.compile(r"statement|account history|export", re.I), "USER_EXPORT"),
]
RETAIN_RE = re.compile(r"retain until\s+(\d{1,2}\s+\w{3,9}\s+\d{4})", re.I)
NAME_LINE = re.compile(r"^(?:Detail|Record Customer)\s+([A-ZÀ-Ý][\w'À-ÿ-]+(?:\s+[A-ZÀ-Ý][\w'À-ÿ-]+){1,3})\s*$",
                       re.M)


def doc_category(text: str) -> str | None:
    for pattern, category in DOC_CATEGORIES:
        if pattern.search(text):
            return category
    return None


def text_records(file_id: str, captured_at: str, path: Path, pages: list[str]) -> list[dict[str, Any]]:
    lines = [ln.strip() for page in pages for ln in page.splitlines() if ln.strip()]
    title = lines[0] if lines else path.stem
    context = [path.stem, title]
    category_hint = doc_category(" ".join([path.stem, title]))
    table_rows = [ln for ln in lines if ln.count("|") >= 2]
    body_text = "\n".join(ln for ln in lines if ln.count("|") < 2)

    doc_fields: dict[str, Any] = {"title": title, "text": body_text}
    for i, m in enumerate(NAME_LINE.finditer(body_text)):
        doc_fields[f"name[{i}]"] = m.group(1)
    if m := RETAIN_RE.search(body_text):
        doc_fields["retain_until"] = m.group(1)

    records = []
    for i, row in enumerate(table_rows):
        row_ids = RESOURCE_RE.findall(row)
        fields: dict[str, Any] = {"title": title, "row": row}
        if m := RETAIN_RE.search(row):
            fields["retain_until"] = m.group(1)
        records.append(make_record(
            file_id=file_id, captured_at=captured_at, source_type="pdf",
            resource_id=row_ids[0] if row_ids else None, fields=fields, extractor="pdf",
            locator=f"table_row[{i}]", explicit_category=None if row_ids else category_hint,
            context=context,
        ))

    body_ids = list(dict.fromkeys(RESOURCE_RE.findall(body_text)))
    for rid in body_ids:
        records.append(make_record(
            file_id=file_id, captured_at=captured_at, source_type="pdf", resource_id=rid,
            fields=doc_fields, extractor="pdf", locator=f"text:{rid}", context=context,
        ))
    if not body_ids:
        records.append(make_record(
            file_id=file_id, captured_at=captured_at, source_type="pdf", resource_id=None,
            fields=doc_fields, extractor="pdf", locator="document", explicit_category=category_hint,
            context=context,
        ))
    return records


def vision_records(file_id: str, captured_at: str, path: Path, pdf: Any,
                   client: llm.VisionClient) -> list[dict[str, Any]]:
    file_hash = cache.sha256_file(path)
    records = []
    for page_no, page in enumerate(pdf.pages, start=1):
        buf = io.BytesIO()
        page.to_image(resolution=RENDER_DPI).original.save(buf, format="PNG")
        payload = run_vision(client, buf.getvalue(), "image/png", file_hash=file_hash, file_id=file_id,
                             prompt_name=PROMPT, part=f"page{page_no}")
        records += payload_to_records(payload, file_id=file_id, captured_at=captured_at, source_type="pdf",
                                      extractor="pdf_vision", locator_prefix=f"page{page_no}.",
                                      context=[path.stem])
    return records


def extract(file_id: str, captured_at: str, path: Path,
            client: llm.VisionClient | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with pdfplumber.open(path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
        if sum(len(p.strip()) for p in pages) >= MIN_TEXT_CHARS:
            return text_records(file_id, captured_at, path, pages), []
        return vision_records(file_id, captured_at, path, pdf, client or llm.VisionClient()), []
