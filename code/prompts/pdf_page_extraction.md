You are transcribing one rendered page of a business document (an invoice or customer statement, a support attachment, a retention or legal register, a compliance journal extract, or similar). The page had no extractable text layer. Your output feeds a privacy audit that matches records to people, so exact transcription matters more than interpretation.

Return exactly one JSON object and nothing else: no prose, no markdown, no code fences.

The object must have this shape:

{
  "screen_title": string,
  "product_area": string,
  "records": [
    {
      "resource_id": string or null,
      "record_kind": string,
      "status_text": string or null,
      "observed_state": "PRESENT" | "ABSENT" | "REDACTED" | "ANONYMIZED" | "ACTIVE" | "DISABLED" | "BLOCKED" | "UNKNOWN",
      "emails": [string],
      "phones": [string],
      "names": [string],
      "user_ids": [string],
      "hashes": [string],
      "aliases": [string],
      "fields": [{"label": string, "value": string}]
    }
  ]
}

Field rules:

- `screen_title`: the document title. `product_area`: the issuing team or system if printed (for example "Finance Operations"), else an empty string.
- One entry in `records` per business record on the page. A register or table produces one entry per row. A single letter, statement or attachment produces one entry.
- `resource_id`: an internal identifier exactly as printed, matching patterns such as `support_001`, `audit_record_003`, `export_1842`, `legal_hold_4743`, `account_5762`. Use null if none is printed. Never invent or guess one.
- `user_ids`: every printed `user_<digits>` token for the record.
- `emails`, `phones`, `names`: personal contact data of the customer or data subject, copied character for character. Staff or approver names go in `fields`.
- `aliases`: business references such as order numbers (`ORD-20264118`), statement numbers (`NST-2026-0718-1842`), ticket numbers (`CS-4827`), journal references (`CJX-20260905-0630`) and file names.
- `status_text` / `observed_state`: as printed. A document that simply contains the person's details is PRESENT. Retention wording such as "Retain until …" stays PRESENT; copy the date into `fields` with label "Retain until".
- `fields`: other label/value pairs relevant to identity, ownership, retention or status (for example "Retain until", "Uploaded by", "Received", "Reason").

Exact copying matters most for identifiers: the audit compares them character by character, so a single dropped or added character turns one person into another. Copy every email address, identifier and reference exactly, including every `.`, `-`, `_` and digit, and copy prefixes as printed (`ORD-20264118`, not `ORDER-20264118`). Do not correct, normalize, or merge values that look similar.

Use empty arrays, not null, for list fields with nothing visible.
