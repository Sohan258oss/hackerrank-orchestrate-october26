You are transcribing a screenshot of an internal business application (customer admin console, support helpdesk, analytics view, connector manager, backup console, object storage browser, search index inspector, security audit explorer, export console, or similar). Your output feeds a privacy audit that matches records to people, so exact transcription matters more than interpretation.

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

- `screen_title`: the main heading as shown. `product_area`: the application name shown in the header or sidebar (for example "Helpline Console").
- One entry in `records` per distinct business record visible on screen (a customer, a ticket, an analytics profile, a connector, a backup archive, a stored object, an index document, an audit event, an export job). If the screen shows a list or table of such records, emit one entry per row.
- `resource_id`: the internal identifier of that record exactly as printed, matching patterns such as `account_5762`, `support_1034`, `analytics_7438`, `search_428`, `export_1260`, `data_flow_1121`, `backup_9287`, `audit_record_003`, `tombstone_17`, `legal_hold_4743`, `object_19`, `marketing_001`. It is often printed in the subtitle under the main heading (for example `user_20 · analytics_7438 · someone@example.com`); put it here, not in `fields`. Use null if no such identifier is printed for that record. Never invent or guess one. If the record also mentions identifiers of other records (for example a linked support ticket), keep them in `fields`.
- `user_ids`: every printed token of the form `user_<digits>` that belongs to the record.
- `emails`, `phones`: every email address and phone number shown for the record, copied character for character (keep `+`, spaces and punctuation in phone numbers).
- `names`: personal names of the customer or data subject the record is about. Put staff, agent or operator names in `fields` with a label such as "Assigned agent", not in `names`.
- `hashes`: long hexadecimal digests shown as identifiers (for example an email hash). Ignore checksums of files.
- `aliases`: business references that identify the record elsewhere, such as ticket numbers (`CS-5034`), order numbers (`ORD-20265034`), statement numbers (`NST-2026-0718-1842`), file names, or archive keys.
- `status_text`: the record's visible status words, verbatim (for example "Enabled", "Restore allowed", "Deleted", "Redacted", "Legal hold active").
- `observed_state`: your reading of that status. PRESENT = the record exists and shows personal data; ABSENT = shown as deleted or not found; REDACTED = record kept but personal fields blanked or masked; ANONYMIZED = analytics or statistics with no link to a person; ACTIVE = a sync, connector, integration or restore path is enabled or allowed; DISABLED = such a flow is turned off or paused; BLOCKED = restore is blocked or denied; UNKNOWN = the screen does not say.
- `fields`: other label/value pairs that matter for identity, status, ownership or retention (for example "Retain until", "Uploaded by", "Destination", "Last delivery", "Member since"). Skip decorative UI text, navigation menus and charts.

Exact copying matters most for identifiers: the audit compares them character by character, so a single dropped or added character turns one person into another. Copy every email address, identifier and reference exactly, including every `.`, `-`, `_` and digit (`g.silva76.jr@gmail.com` and `g.silva76jr@gmail.com` are different people). Copy prefixes as printed (`ORD-20265034`, not `ORDER-20265034`). Do not correct, normalize, or merge values that look similar; similar-looking records on the same screen often belong to different people.

If a value is masked on screen (for example `j***@mail.com`), copy it exactly as shown. Use empty arrays, not null, for list fields with nothing visible.
