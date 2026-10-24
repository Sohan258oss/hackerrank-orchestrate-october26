# Rules derived from the 32 public samples

Source material: `dataset/sample_deletion_requests.csv`, `dataset/sample_output.csv`, and the
participant-facing context files (`user_identities.csv`, `user_audit_log.csv`,
`system_activity.csv`, `deletion_rules.csv`, `evidence_files.csv`, `dataset/evidence/**`).

The per-case walkthrough (Part 1) explains the public answers. The rules (Part 2) are written to be
general: they name evidence *patterns*, never specific case, user, resource or file IDs as answers.
Anything marked **Verified** was checked programmatically against all 32 sample rows; anything
marked **Hypothesis** fits the samples but is not proven; **Open** items are unresolved.

Sample status mix: 10 `COMPLETE`, 22 `INCOMPLETE`, 0 `NOT_APPLICABLE`.

---

## Part 1 — Case-by-case walkthrough

Notation: `F` = FINDING, `R` = REMEDIATION, `V` = VERIFICATION. "ev" = `evidence_id`; `—` = empty
string. `audit_at` is abbreviated `A`.

### Common shape of every answer

* **COMPLETE** → only `V` steps, every one citing the evidence that proves the state at `A`.
* **INCOMPLETE** → `F` steps (each with ev) → `R` steps (ev `—`) → `V` steps (ev `—`).
  The `V` block covers the final state of **every relevant resource**, not just the ones remediated
  (the deleted account and the retained security audit record are re-verified too).
* Records that are already compliant in an INCOMPLETE case get **no** finding and **no** remediation,
  only their `V` step.

### Per-case table

| Case | Request (lang) | Subject resolution | Status | Steps and why |
|---|---|---|---|---|
| 01 | EN, "would like it closed… remove the profile" | current email → user_01 | COMPLETE | V ABSENT account (ev = SQL failover-replica snapshot captured before A). The account row is missing from **both** the primary and failover tables, so the snapshot proves absence everywhere. The activity said SUCCESS, but that is only a claim; the snapshot is the proof. |
| 02 | ES, "intenté eliminar mi cuenta… completar la eliminación" | current | COMPLETE | Same proof as 01. The activity result was `PARTIAL` and the reported status `PARTIALLY_COMPLETE`, yet the true state is absent, so COMPLETE. Shows that claims can understate as well as overstate. |
| 03 | FR, "suppression définitive" | current | COMPLETE | Three V steps, all citing the latest readback log before A: account lookup `rows=0 http_status=404` → ABSENT; audit record `rows=1 … retention_until=2034…` → PRESENT (SECURITY_AUDIT_LOG, RETAIN); analytics profile with every identifier `null` → ANONYMIZED. |
| 04 | EN, deleting account, "anonymous statistics OK" | current | COMPLETE | V ABSENT account (failover snapshot); V ANONYMIZED analytics (analytics report JSON with `user_id/email/phone/email_hash = null`). The customer explicitly allows anonymization rather than deletion, consistent with rule_07. |
| 05 | EN, "same email is still recognized" | current | INCOMPLETE | **Replica drift**: the account is absent from `customer_profiles_primary` but still `ACTIVE` with full PII in `customer_profiles_failover_replica`. F REMAINING_RECORD account (ev = that snapshot) → R DELETE → V ABSENT. A `SYNC_RECORD … SUCCESS` activity to the failover replica is a claim that the snapshot contradicts. `system_id` stays the logical `account_service`, not the replica table name. |
| 06 | EN, old profile still in search | current | INCOMPLETE | Search document with name + email in the search primary SQL snapshot → F REMAINING (SEARCH_INDEX_ENTRY → DELETE) → R DELETE → V ABSENT account (re-verified, ev —) + V ABSENT search. |
| 07 | EN, delete account + identity document sent to support | current | INCOMPLETE | PDF support attachment (captured < A) shows the subject's name, email and phone on a document uploaded by the customer. It belongs only to the subject (SUPPORT_ATTACHMENT rule_06 → DELETE). F REMAINING support (ev = PDF file_id) → R DELETE → V ABSENT account + support. |
| 08 | EN, old "download your data" link still works | current | INCOMPLETE | Export-jobs JSON shows an export owned by the subject's email (USER_EXPORT → DELETE). The activity said FAILED. F REMAINING export → R DELETE → V ABSENT account, export. |
| 09 | EN, permanent deletion | current | INCOMPLETE | Inventory SQL snapshot (captured before A) has a `backup_restore_entries` row with the subject's email and `restore_allowed=1` → F RESTORE_RISK → R BLOCK_RESTORE → V ABSENT account, V PRESENT audit record, V RESTORE_BLOCKED. A structured log captured **after** A is not cited. The backup is retained (rule_14), never deleted, and gets no `VERIFY_RECORD_PRESENT`. |
| 10 | EN, "used this Yahoo address before changing" | **historical** email (audit log, two hops of email changes) → user | INCOMPLETE | Support replica log: `contact_query … lookup_key=<historical email> source=legacy_contact_index` → `rows=1`, `deletion_marker_check … marker_found=false`. A legacy contact-index entry keyed on an old email → DELETE category → F REMAINING → R DELETE → V ABSENT account, support. A neighbouring row for a *similarly named* person is a decoy. |
| 11 | EN, erase + handle copies | current | INCOMPLETE | Shift-handover TXT captured exactly at A (inclusive): search FOUND → REMAINING; analytics IDENTIFIABLE → EXPOSED_PII; support message IDENTIFIABLE → EXPOSED_PII; data flow ACTIVE → ACTIVE_DATA_FLOW. Remediation: DISABLE flow → DELETE search → REDACT support → ANONYMIZE analytics. Verification lists all six resources, including the account (NOT_FOUND) and the audit record (FOUND), with ev —. |
| 12 | EN, changed email; check systems "that do not display the email" | current; history gives the **old email hash** | INCOMPLETE | Service log line `analytics_platform … contact_lookup_completed rows=1 email_opt_in=true correlation_key="<old email_hash>"`. The record carries only a hashed former identifier and is a contact/marketing-style entry, so it is a DELETE category (SECONDARY_IDENTIFIER / MARKETING_PROFILE), **not** ANONYMIZE. F REMAINING → R DELETE → V ABSENT account, analytics; V PRESENT audit. |
| 13 | EN, close account | current | INCOMPLETE | Handover: search FOUND (DELETE); support item typed `SUPPORT_ATTACHMENT` FOUND (subject-only → DELETE). Both are REMAINING → both DELETE → V ABSENT account, search, support; V PRESENT audit. |
| 14 | JA, 完全な削除 | current | INCOMPLETE | Handover: `BACKUP_ARCHIVE | ACTIVE` → F RESTORE_RISK → R BLOCK → V ABSENT account, PRESENT audit, RESTORE_BLOCKED. |
| 15 | EN, an integration still active | current | INCOMPLETE | Service log: connector heartbeat `enabled=true scheduler_registered=true last_run=success`. It is tied to the subject only via the **trace_id** whose earlier line has `subject_key="<email>"`. F ACTIVE_DATA_FLOW → R DISABLE → V ABSENT account, PRESENT audit, DATA_FLOW_DISABLED. |
| 16 | EN, permanent deletion incl. earlier identifiers | current | INCOMPLETE | Read-replica SQL: the account exists **only in the replica table** (drift, as in 05), and the search doc exists in both primary and replica. F REMAINING account + search → R DELETE both → V ABSENT both + PRESENT audit. |
| 17 | EN, "may retain backups… must not restore my profile" | current | INCOMPLETE | Service log `restore_candidate_evaluated eligible=true deletion_block=false` (trace-linked to the email) → F RESTORE_RISK → R BLOCK → V… An older (pre-request) readback hit for a *different* account under the same email, and an old tombstone link, are **not** in scope: the same subject has another request in the evaluation set. |
| 18 | EN, remove account + identifiers | current (phone changed earlier) | INCOMPLETE | Readback log captured before A: search hit with email → REMAINING; analytics with `email=<email>` → EXPOSED_PII; support with `requester_email` → EXPOSED_PII (REDACT, shared context); connector `enabled=true last_delivery_status=delivered` → ACTIVE_DATA_FLOW. Same 4F/4R/6V template as 11. |
| 19 | EN, clean details from support conversations that involve another customer | current | INCOMPLETE | Log `message_pii_scan matched_value="<email>"` on the support message → EXPOSED_PII → REDACT (rule_05: shared context must be retained). Search hit → REMAINING → DELETE. V includes REDACTED support. |
| 20 | EN, permanent deletion | current | INCOMPLETE | Readback log: search hit → REMAINING; connector enabled+delivered → ACTIVE_DATA_FLOW. R order: DISABLE flow, then DELETE search. |
| 21 | EN, verify profile gone and security retention minimal | current | COMPLETE | Service-resource export JSON (captured < A): account `status_code 404, profile null` → ABSENT; audit record with `retention_until` in the future → PRESENT; analytics all-null → ANONYMIZED. A later snapshot bundle (captured after A) also lists them but is not cited. |
| 22 | EN, erase | current | INCOMPLETE | Read-replica SQL: search doc (primary + replica) → REMAINING; analytics JSON payload still has `user_id`, email, phone, hash → EXPOSED_PII → ANONYMIZE. |
| 23 | EN, delete account, export archive and search result; security records may stay | current | INCOMPLETE | Export JSON: export with `owner_email` → REMAINING; search hit → REMAINING. Two DELETEs, V ABSENT ×3 + PRESENT audit. The audit record is only visible in a bundle captured **after** A, yet it is still in the V block (ev —). See Open-2. |
| 24 | EN, told complete but account + search still found | current | INCOMPLETE | Export JSON: account lookup `status_code 200` with `email` still populated (other fields null). A partially stripped profile is still a remaining USER_PROFILE → DELETE. Search hit → DELETE. |
| 25 | EN, "page says failed, inspect actual systems" | current (phone changed) | COMPLETE | Despite `NOT_STARTED` and a FAILED delete activity, the export shows a 404 account, a retained audit record and an all-null analytics profile → 3 V steps citing that export. |
| 26 | EN, old report said account existed | current | COMPLETE | Export: 404 account; audit PRESENT; support ticket with `requester_*` null but `customer_reference` kept → REDACTED; analytics all-null → ANONYMIZED; connector `enabled:false, stopped` → DATA_FLOW_DISABLED; backup `restore_allowed:false` → RESTORE_BLOCKED. No `VERIFY_RECORD_PRESENT` for the backup. |
| 27 | EN, audit "as it stood when reviewed" | current | COMPLETE | Same six-check shape as 26, all from the export captured before A. A bundle captured after A is ignored even though it covers the same resources. |
| 28 | EN, a similar-email search result is someone else's | current | COMPLETE | Same shape as 26. The similar-looking search document (a different email) is a **decoy** and must not be attributed. |
| 29 | EN, "used a different email before", disable integrations | **historical** email → user | INCOMPLETE | Every exposed record uses the **old** email (search hit, analytics `email`, support `requester_email`, connector `customer_email` + `enabled:true`). 4F/4R/6V template. |
| 30 | EN, traces in search/support/analytics, disable sync | **historical** email → user | INCOMPLETE | Same pattern as 29. |
| 31 | EN, "keeping an internal deleted-account reference is fine" | current | COMPLETE | Snapshot bundle captured exactly at A: the `account_service/customer_profiles` snapshot has **0 records** → ABSENT; audit PRESENT; `identity_service/deleted_customer_references` row with `subject_reference` kept and name/email/phone/hash `null`, `account_status=DELETED` → **ACCOUNT_TOMBSTONE** correctly REDACTED. |
| 32 | EN, deleted-account record still shows details | current | INCOMPLETE | The same bundle shows the tombstone with name, email, phone and email_hash still populated → F EXPOSED_PII (identity_service) → R REDACT → V ABSENT account, PRESENT audit, REDACTED tombstone. The tombstone is **not** deleted. |

---

## Part 2 — General rules

### (a) Deletion intent vs `NOT_APPLICABLE`

* All 32 samples authorize deletion. The evaluation set adds languages (ES, PT, FR, JA, KO) and a small
  set of non-deletion requests. One `CLASSIFY_REQUEST … classified as non-deletion` activity exists for
  each, which corroborates but must not replace text understanding.
* **Deletion** = the requester asks for the account and/or personal data to be deleted, erased,
  removed or closed with deletion, in any language. Signals include delete, erase, remove,
  "close … and delete", eliminar/exclusão/suppression/削除/삭제. A complaint that a prior
  deletion did not finish (samples 02, 05, 24, 25) is still a deletion request.
* **Not deletion → `NOT_APPLICABLE`, `[]`**: correction or update (phone, name, email change), access
  or copy-of-data ("what do you hold", "send me a copy"), export or portability, marketing opt-out
  ("remove me from marketing lists **only**", "stop promotional emails"), login help, invoices.
* **Negation traps**: non-deletion texts often contain deletion words in negated or limited form,
  e.g. "not asking for deletion", "don't delete", "do not close", "without removing",
  "keep the account". Classify on the **requested action**, not on keyword presence. Explicit
  statements that the person wants to keep or keep using the account dominate.
* Partial-scope permission is **not** non-deletion. "You may retain backups", "anonymous statistics
  are fine" and "keep an internal reference" are deletion requests whose wording matches the policy
  treatments (RETAIN / ANONYMIZE / tombstone REDACT). Policy, not the text, decides each treatment.
* Recommended implementation: an LLM classifier with a strict JSON output (`authorizes_deletion: bool`),
  backed by a deterministic negation and keyword check. Disagreements go to the LLM with a rationale.

### (b) Identity resolution

* **Verified**: `email_hash = sha256(lowercase(email))` for all 850 current rows, and every audited
  old `email_hash` equals the sha256 of the old email changed at the same `event_time`.
* Algorithm:
  1. Normalize the request email (`strip().lower()`). Match `user_identities.email`
     case-insensitively; evidence also contains UPPERCASED emails.
  2. If there is no current match, match `user_audit_log.old_value` (and `new_value`) where
     `field_name='email'`. Follow chains: a user can have several email changes
     (sample 10 has two hops).
  3. The identifier set for the subject is: `user_id`; current email, phone and email_hash; every
     `old_value`/`new_value` for email, phone and email_hash in the audit log; and the sha256 of
     every known email (covers any missing hash rows). Phones should be compared after stripping
     spaces and punctuation.
  4. Names are **not** identifiers for matching (many users share surnames, and decoys use similar
     names and similar emails). Use the name only to confirm a match already made on a strong key.
* Evidence references the subject by: email (any casing), `customer_reference`/`subject_reference`/
  `user_id`/`lookup_key`/`subject_key`/`correlation_key` = user_id, email or email hash. Logs often
  put the identifier on a *start* line and the `resource_id`+state on the *completed* line, so the
  two must be joined by `trace_id`.
* Use only identity changes with `event_time <= audit_at`, but keep old identifiers. Old values stay
  relevant because they may persist in systems that were never cleaned.

### (c) State at `audit_at`

* **Evidence cut-off (Verified)**: every cited `file_id` has `captured_at <= audit_at`, and the bound is
  inclusive (samples 11 and 31 cite files captured exactly at `audit_at`). Use the manifest
  `captured_at`, not timestamps inside the file. Sample 09 ignores a log whose lines are timestamped
  before A but whose file was captured after A.
* **Latest wins (Verified)**: for every cited resource, the cited file is the **most recent** evidence
  file captured at or before A that mentions that resource. Older observations are superseded; later
  ones are invisible.
* Activity rows are filtered the same way (`event_time <= audit_at`).
* **Claims vs state**: `reported_status` (COMPLETE / PARTIALLY_COMPLETE / NOT_STARTED) and
  `reported_result` (SUCCESS / FAILED / PARTIAL) are never proof. Samples show all mismatch
  directions: reported COMPLETE but data remains (05, 11, 24); reported NOT_STARTED or a FAILED delete
  but the record is actually gone (25, 26, 28); PARTIAL but done (02). A `CHECK_DATA_FLOW … remains
  enabled` or `CHECK_RESTORE_ACCESS … can still be restored` activity is a hint at a risk, but the
  cited evidence is still the file that shows the state.
* **State extraction by evidence form**:

  | Evidence pattern | State |
  |---|---|
  | profile lookup `rows=0`, `http_status=404`, `status_code: 404`, `profile: null`, `NOT_FOUND` | ABSENT |
  | full-table snapshot whose scope covers the store and has no row for the resource | ABSENT (see (d) and Open-1) |
  | row/record present with any direct identifier (name, email, phone, email hash, user_id in a delete-category store) | PRESENT / remaining |
  | `FOUND`, `result=hit`, `total_hits>=1`, `rows=1`, `marker_found=false` | PRESENT |
  | `IDENTIFIABLE`, analytics/support record with email/phone/name/hash/user_id populated | exposed PII |
  | analytics with all identifier fields null, `ANONYMIZED`, "no direct identifier" | ANONYMIZED |
  | support with `requester_*` null but `customer_reference` kept | REDACTED |
  | tombstone with `subject_reference` kept and name/email/phone/hash null | REDACTED |
  | connector `enabled=true`/`1`, `ACTIVE`, `scheduler_registered=true`, `last_delivery_status=delivered` | flow ACTIVE |
  | connector `enabled=false`/`0`, `DISABLED`, `stopped` | flow DISABLED |
  | backup `restore_allowed=true`/`1`, `eligible=true deletion_block=false`, `BACKUP_ARCHIVE ACTIVE` | restore risk ACTIVE |
  | backup `restore_allowed=false`/`0`, `BLOCKED` | restore BLOCKED |
  | audit record `rows=1`/`FOUND` with `retention_until > audit_at` | retained PRESENT |

* **Case scope (Hypothesis, strongly supported)**: the relevant resources are
  1. the resources of the case's own deletion batch in `system_activity` (`source_system=deletion_worker`),
     which cluster around `reported_at`: the primary-profile delete about 45–75 min before it, successful
     policy actions at fixed offsets (≈ −29, −22, −15, −8 min), failed or check events after it;
  2. resources tied to the subject's identifiers in evidence captured inside **[requested_at, audit_at]**;
  3. the subject's retained security audit records (`subject_reference = user_id`).

  Checked on all 32 samples: no identifier match inside that window was ever outside the gold set,
  while a match from *before* `requested_at` (sample 17) was correctly excluded. Some subjects
  appear in two cases (one sample and one evaluation request), so stale matches belong to the other
  case.

### (d) Replica drift in SQL snapshots

* SQL files hold a `*_primary` table plus one or more replica tables (`read_replica`,
  `failover_replica`, `reporting_replica`, `replica_<region>`) or, for inventories, a
  `snapshot_scope` table listing which origin systems were captured. Load each with SQLite and
  compare tables by `resource_id`.
* A resource is deleted only if it is absent from **every** copy. Present in a replica but missing
  from the primary is **drift** → `REPORT_REMAINING_RECORD` (or `REPORT_EXPOSED_PII` for redact/anonymize
  categories), cited with the SQL `file_id` (samples 05, 16).
* The step's `system_id` is the **logical owning service** (`account_service`, `search_service`,
  `analytics_platform`, …, taken from the `data_store` column or the table family), never the replica
  table name. Replication is not a separate step in the samples; deleting the record means deleting
  every copy.
* Check JSON payload columns (`payload` holding a JSON document) for identifier fields; a row can exist
  with identifiers already nulled (compliant for redact/anonymize categories).
* `SYNC_RECORD … reported its final checkpoint` activity after a delete is a recreation signal and a
  reason to inspect the replica. Restore jobs (`RESTORE_BACKUP … repopulated`) are another.
* Absence proof: a complete snapshot of the store, captured before A, with no row in primary **or**
  replica (01, 02, 04). An inventory with an empty `customer_profiles` table whose `snapshot_scope`
  includes `account_service` also qualifies (31 uses an empty bundle snapshot). See Open-1 for which
  file to prefer.

### (e) `deletion_rules.csv` → treatment

| Category | Rule | Violation finding | Remediation | Final verification |
|---|---|---|---|---|
| USER_PROFILE (account_service) | DELETE | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| SECONDARY_IDENTIFIER (e.g. legacy contact index, hashed-key lookups) | DELETE | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| SEARCH_INDEX_ENTRY | DELETE | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| CACHE_ENTRY | DELETE | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| SUPPORT_ATTACHMENT owned only by subject | DELETE | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| USER_EXPORT | DELETE | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| MARKETING_PROFILE | DELETE | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| SUPPORT_MESSAGE with shared context | REDACT | REPORT_EXPOSED_PII | REDACT_RECORD | VERIFY_RECORD_REDACTED |
| EMAIL_DELIVERY_LOG containing an email | REDACT | REPORT_EXPOSED_PII | REDACT_RECORD | VERIFY_RECORD_REDACTED |
| ACCOUNT_TOMBSTONE | REDACT (keep `subject_reference`) | REPORT_EXPOSED_PII | REDACT_RECORD | VERIFY_RECORD_REDACTED |
| ANALYTICS_PROFILE / PRODUCT_TELEMETRY attributable | ANONYMIZE | REPORT_EXPOSED_PII | ANONYMIZE_RECORD | VERIFY_RECORD_ANONYMIZED |
| SECURITY_AUDIT_LOG within retention | RETAIN | — (never a finding) | — | VERIFY_RECORD_PRESENT |
| LEGAL_HOLD_RECORD with active hold | RETAIN | — | — | VERIFY_RECORD_PRESENT (by analogy; no sample) |
| BACKUP_ARCHIVE where restore can be blocked | RETAIN + block | REPORT_RESTORE_RISK if restorable | BLOCK_RESTORE | VERIFY_RESTORE_BLOCKED only (no PRESENT step in samples) |
| Data flow / connector | (not a data category) | REPORT_ACTIVE_DATA_FLOW if active | DISABLE_DATA_FLOW | VERIFY_DATA_FLOW_DISABLED |

Notes:

* **Category comes from the record, not from the system.** An `analytics_platform` record that is an
  email-opt-in contact keyed by a (former) email hash was treated as DELETE (sample 12), while
  ordinary analytics profiles are ANONYMIZE. A `support_platform` item typed SUPPORT_ATTACHMENT, an
  attachment document, or a legacy contact-index entry is DELETE; a ticket/message with
  agent/order context is REDACT. Use the explicit category column when evidence has one (handover TXT,
  connector JSON); otherwise infer from collection/event names (`attachment_*`, `message_pii_scan`,
  `contact_lookup … email_opt_in`, `legacy_contact_index`).
* A redact/anonymize-category record that still exposes identifiers gets `REPORT_EXPOSED_PII`, not
  `REPORT_REMAINING_RECORD`. A delete-category record gets `REPORT_REMAINING_RECORD` even if only one
  identifier remains (24: profile with only `email` left).
* **ACCOUNT_TOMBSTONE** (`identity_service`, `deleted_customer_references`): keep the record and its
  stable `subject_reference`/user_id; name, email, phone and email_hash must be null. Never
  `DELETE_RECORD` a tombstone, and never treat the kept user_id as exposed PII. The same reasoning
  explains why support records may keep `customer_reference` once redacted.
* Retained records (audit logs, legal holds, backups) are never deleted. Deleting them would itself be
  wrong.
* A resource the subject explicitly disowns ("belongs to someone else"), or one matched only by a
  similar name or email, is out of scope.

### (f) When to emit active-data-flow and restore-risk findings

* `REPORT_ACTIVE_DATA_FLOW` (state `ACTIVE`): a connector or data flow linked to the subject (by
  `customer_email`, `customer_reference`, `subject_key`/trace, or a handover row) whose latest
  pre-A state is enabled/active. `last_delivery_status=delivered` or `last_run=success` reinforce it.
  Emit one per flow → `DISABLE_DATA_FLOW` → `VERIFY_DATA_FLOW_DISABLED`.
* `REPORT_RESTORE_RISK` (state `ACTIVE`): a backup/restore entry linked to the subject whose latest
  pre-A state allows restore (`restore_allowed` true/1, `eligible=true` with `deletion_block=false`,
  handover `BACKUP_ARCHIVE | ACTIVE`). Emit → `BLOCK_RESTORE` → `VERIFY_RESTORE_BLOCKED`. Do not
  delete the backup.
* If the flow is disabled or the restore blocked at A, there is no finding; emit only the `VERIFY_*`
  step, citing that evidence when the case is COMPLETE.
* Either risk alone makes the case INCOMPLETE even when every record is clean (samples 09, 14, 15, 17).
* `CHECK_DATA_FLOW` / `CHECK_RESTORE_ACCESS` activities ("remains enabled" / "can still be restored") are
  pointers to the risk; cite the evidence file showing the state at A when one exists.
* Recreation signals worth investigating even without a connector: `SYNC_RECORD` into replicas after
  a delete, and `RESTORE_BACKUP … repopulated downstream resource`.

### (g) Ordering

* **Verified on 32/32 samples**: within each `steps_json`, steps are sorted strictly by
  `(operation_rank, system_id, resource_id)` using string order, where `operation_rank` follows the
  operation table:

  1. REPORT_REMAINING_RECORD 2. REPORT_EXPOSED_PII 3. REPORT_ACTIVE_DATA_FLOW 4. REPORT_RESTORE_RISK
  5. DISABLE_DATA_FLOW 6. BLOCK_RESTORE 7. DELETE_RECORD 8. REDACT_RECORD 9. ANONYMIZE_RECORD
  10. VERIFY_RECORD_ABSENT 11. VERIFY_RECORD_PRESENT 12. VERIFY_RECORD_REDACTED
  13. VERIFY_RECORD_ANONYMIZED 14. VERIFY_DATA_FLOW_DISABLED 15. VERIFY_RESTORE_BLOCKED

  This rank order implies the phase order FINDING → REMEDIATION → VERIFICATION. It also puts
  "disable flows and block restores before touching records" first in remediation.
* `step_no` runs 1..n consecutively after sorting.
* "Downstream copies before their sources" (AGENTS §6.5) never conflicts with the sort in the samples,
  because replica copies are reported under the owning service's single resource_id. If a case ever
  needs two DELETE steps where one record feeds the other (e.g. a search document derived from an
  account), the sort already puts `account_service` before `search_service` alphabetically. Keep the
  plain sort as the default, and only reorder when evidence shows an explicit source→copy dependency
  that the sort would violate (no such case is observed).
* Resource IDs are compared as strings (`search_1007` < `search_898`), not numerically.

### (h) `evidence_id` rules

* Allowed values: an evidence `file_id`, an `activity_id`, or an `audit_id`. In the samples every
  citation is a `file_id`. Activity IDs record claims, so prefer the file that shows the state; reserve
  `activity_id` for cases where no file covers the resource and the activity is itself the state proof.
* Each `FINDING` cites the evidence proving the violation at A: the **latest** file captured
  `<= audit_at` that mentions the resource (Verified).
* COMPLETE cases: every `VERIFY_*` cites the latest pre-A file proving that resource's compliant
  state. Several steps may share one file.
* INCOMPLETE cases: every REMEDIATION and every VERIFICATION step has `evidence_id = ""`, including
  verification of resources that were already compliant (Verified).
* Never cite a file captured after A, even if its in-file timestamps predate A (Verified).
* Image and PDF evidence are cited by `file_id` like any other file (sample 07 cites a PDF). Facts must
  be extracted by OCR or vision. The `LINK_RESOURCE_IDENTITY` activities that confirm a resource/subject
  pairing for screenshots are corroboration; the screenshot or document is what proves the state.

---

## Part 3 — Open questions and risks

* **Open-1: choosing the absence-proof file.** Samples 01, 02 and 04 cite the failover-replica
  snapshot (primary + replica both lacking the row) even where a later empty inventory snapshot of
  `account_service` (captured before A) exists. Working rule: prefer a file that names the resource
  (404 / NOT_FOUND / `profile: null`). Otherwise prefer a snapshot that covers primary **and** replica
  copies of the store. Otherwise use the latest empty-scope snapshot.
* **Open-2: retained records seen only after A.** Some INCOMPLETE cases list
  `VERIFY_RECORD_PRESENT` for a security audit record that appears only in evidence captured after A
  (09, 23, 24, 29, 30). Because these are future checks with an empty `evidence_id`, the gold
  evidently includes the subject's retained audit record regardless. Rule: include
  `VERIFY_RECORD_PRESENT` for the subject's in-retention audit records. Never let post-A evidence
  change the status or the findings.
* **Open-3: linking identifier-free resources.** A deleted account (404) or an already-anonymized
  analytics record carries no identifier, so it can only be tied to the case through its deletion
  batch. When several cases share the same `reported_at`, their batches interleave and the anonymize
  steps occur within seconds of each other. In the samples, neither time order nor resource-id order
  separates them reliably. Mitigations: use every positive link first (trace joins, older snapshots
  where the record still had identifiers, screenshots plus `LINK_RESOURCE_IDENTITY`, inventory rows),
  then eliminate resources already claimed by other cases, and only then fall back to batch timing.
* **Open-4: delete vs anonymize for analytics.** Decided by record type (contact/opt-in or
  hashed-identifier entry → DELETE; behavioural profile → ANONYMIZE). Only one sample shows the
  DELETE variant, so this boundary needs custom evaluation cases.
