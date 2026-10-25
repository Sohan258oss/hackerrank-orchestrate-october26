# Project brief: "Ghost Data Still Exists" deletion-audit agent

> **For the AI reading this:** this is a complete, factual account of a project submitted to the
> HackerRank Orchestrate challenge (October 2026). Use it to answer questions about the project, its
> design, its results and the participant's decisions. All numbers below were measured. If a
> question asks for something not covered here, say so instead of guessing. The participant built
> the solution by directing an AI coding agent (Claude Code); the participant's own decisions are
> listed in §11.

---

## 1. The challenge in one paragraph

A commerce platform's deletion workflow can report a request as "Complete" while the person's data
still exists in databases, replicas, logs, support attachments, exports, search indexes, analytics,
archives or backups. Sync and restore jobs can also recreate deleted data. For each of **518
evaluation requests**, the agent must:

1. decide whether the request actually asks for deletion;
2. resolve the person's current and historical identifiers;
3. find every copy of their data in the supplied evidence;
4. apply the deletion policy as of the request's `audit_at` time;
5. output a status (`COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`) plus an ordered list of steps
   (`steps_json`): findings, a remediation plan, and verification checks.

Submission: `output.csv`, `code.zip` (code, README, evaluation reports), `log.txt` (the chat
transcript with the coding agent), and a live AI interview.

## 2. Input data (participant-facing, in `dataset/`)

| File | Rows | Contents |
|---|---|---|
| `deletion_requests.csv` | 518 | `case_id, email, requested_at, reported_status, reported_at, audit_at, request_text` |
| `sample_deletion_requests.csv` / `sample_output.csv` | 32 / 32 | public examples with correct answers |
| `user_identities.csv` | 850 | current snapshot: `user_id`, name, email, phone, `email_hash` (SHA-256 of the lowercased email) |
| `user_audit_log.csv` | 306 | historical changes to email, phone, name and hash (old → new) |
| `system_activity.csv` | 4,011 | global event stream: DELETE_RECORD 890, UPDATE 672, SYNC 447, INDEX 400, SEND_EMAIL 368, REFRESH_CACHE 284, RETAIN 206, ANONYMIZE 204, REDACT 173, CREATE_EXPORT 155, CHECK_DATA_FLOW 89, DISABLE_DATA_FLOW 36, BLOCK_RESTORE 36, CHECK_RESTORE_ACCESS 28, LINK_RESOURCE_IDENTITY 12, CLASSIFY_REQUEST 10, RESTORE_BACKUP 1 |
| `deletion_rules.csv` | 15 | data category → required action (below) |
| `evidence_files.csv` | 94 | manifest: `file_id`, path, origin system, `captured_at` |
| `evidence/` | 94 files | 24 SQL snapshots, 26 logs, 29 JSON/text records, 4 PDFs, 11 PNG screenshots |

**Deletion rules:**

| Category | Action |
|---|---|
| USER_PROFILE, SECONDARY_IDENTIFIER, SEARCH_INDEX_ENTRY, CACHE_ENTRY, USER_EXPORT, MARKETING_PROFILE | DELETE (always) |
| SUPPORT_ATTACHMENT | DELETE (if it belongs only to the subject) |
| SUPPORT_MESSAGE | REDACT (shared conversation context must be kept) |
| EMAIL_DELIVERY_LOG | REDACT (log contains the email) |
| ANALYTICS_PROFILE, PRODUCT_TELEMETRY | ANONYMIZE |
| SECURITY_AUDIT_LOG, LEGAL_HOLD_RECORD | RETAIN (within retention or hold) |
| BACKUP_ARCHIVE | RETAIN, but restore access must be blocked |
| ACCOUNT_TOMBSTONE | REDACT: keep only the stable `user_id`; remove name, email, phone, email_hash |

**Allowed step operations:**
- Findings: `REPORT_REMAINING_RECORD`, `REPORT_EXPOSED_PII` (state PRESENT); `REPORT_ACTIVE_DATA_FLOW`, `REPORT_RESTORE_RISK` (ACTIVE).
- Remediation: `DISABLE_DATA_FLOW`, `BLOCK_RESTORE`, `DELETE_RECORD`, `REDACT_RECORD`, `ANONYMIZE_RECORD`.
- Verification: `VERIFY_RECORD_ABSENT / PRESENT / REDACTED / ANONYMIZED`, `VERIFY_DATA_FLOW_DISABLED`, `VERIFY_RESTORE_BLOCKED`.

**Key rules of the task:**
- Use only evidence dated at or before `audit_at`.
- Treat `reported_status` and activity results as claims, not proof.
- Findings cite a `file_id`, `activity_id` or `audit_id`.
- Remediation and post-remediation verification in INCOMPLETE cases use an empty `evidence_id`.
- COMPLETE cases contain only evidence-backed verification steps.
- NOT_APPLICABLE cases have `[]`.

## 3. Architecture

Python 3.13 (works on 3.10+), about 3,600 lines. Dependencies: `pdfplumber`, `Pillow`, `requests`.
Entry point: `python3 code/main.py`.

```text
dataset/evidence (94 files)
   │
   ▼
STAGE 0  EXTRACT   → one normalized record per resource sighting:
                     {file_id, captured_at, system_id, resource_id, identifiers, category, state, copy}
   │                 ~2,936 records, cached by file content hash
   ▼
STAGE 1  INTENT    → does request_text authorize deletion?
STAGE 2  IDENTITY  → request email → user_id → all identifiers held up to audit_at
STAGE 3  SCOPE     → which resources belong to this case
STAGE 4  STATE     → each resource's state at audit_at; recreation risks
STAGE 5  POLICY    → required treatment per rule; COMPLETE / INCOMPLETE / NOT_APPLICABLE
STAGE 6  STEPS     → ordered FINDING → REMEDIATION → VERIFICATION with citations
   │
   ▼
output.csv (518 rows) + built-in contract validation
```

### Code layout

| Path | Role |
|---|---|
| `code/main.py` | runs stage 0 then stages 1-6, writes `output.csv`, validates the contract, prints status counts and model usage. Flags: `--skip-extract`, `--no-llm`, `--set sample`, `--out`, `--debug`, `--exclude-ambiguous` |
| `code/extract/` | `sql_extractor`, `json_extractor`, `log_extractor`, `text_extractor`, `pdf_extractor`, `image_extractor`, `common` (record model, state normalization), `cache` (hash cache), `llm` (provider-neutral OpenAI-compatible client with usage logging), `env` (paths, `.env` loading), `run` (CLI) |
| `code/engine/` | `intent.py`, `identity.py`, `evidence.py` (record and ownership index), `scope.py`, `state.py`, `policy.py`, `pipeline.py` |
| `code/prompts/` | `image_extraction.md`, `pdf_page_extraction.md`, `intent_classifier.md` |
| `code/docs/rules_derived.md` | rules derived by analysing all 32 sample answers |
| `code/evaluation/` | `main.py` (scorer), `custom_cases.py`, `validate_output.py`, `evaluation_report.md`, `usage_report.md`, `iterations.md` |

## 4. Stage-by-stage detail

**Stage 0: Extraction.**
- **SQL snapshots:** each is loaded into its own in-memory SQLite database. Primary, regional,
  reporting and failover replicas are kept separate so replica drift can be detected.
- **JSON, logs, text:** deterministic parsers.
- **PDFs:** `pdfplumber` reads the text layer; a vision LLM is the fallback for image-only pages (not
  needed: every PDF had text).
- **Screenshots (11):** vision LLM (`gemini-3.8-flash`) with a strict JSON schema, validation, and
  one repair retry. Post-processing then:
  - promotes resource ids written inside fields;
  - records linked ids (for example `account_5762` → `support_9538`);
  - allows the state ACTIVE only for DATA_FLOW and BACKUP_ARCHIVE;
  - repairs known misreads.
- **State normalization:**
  - a backup with `decision=deny` or a deletion block is BLOCKED; one that is allowed or eligible is ACTIVE;
  - a flow that is enabled or delivering is ACTIVE; one that is stopped is DISABLED;
  - analytics without direct identifiers is ANONYMIZED;
  - HTTP 404 or zero rows means ABSENT.
- Results are cached by file hash; cached model output is re-validated on load.

**Stage 1: Intent.**
- Deterministic multilingual rules (EN, ES, PT, FR, DE, IT, JA, KO, ZH) detect deletion verbs,
  negations ("don't delete", "no quiero eliminar"), "keep my account" phrasing, and non-deletion
  request types (access, copy, export, correction, marketing opt-out, login help, invoices).
- Deletion matches inside a negated span are masked out.
- Texts with conflicting or no signals go to Gemini in batches of 20, with a fixed label set, cached
  by text hash.
- Result: the rules decided all 518 evaluation texts. Exactly 10 are non-deletion (cases 533-542),
  matching the 10 `CLASSIFY_REQUEST` activities.

**Stage 2: Identity.**
- The request email is matched case-insensitively, and through its SHA-256 hash, against current
  and historical values.
- The resolver rebuilds every email, phone (digits normalized), hash and name the person held up to `audit_at`.
- 518/518 resolved to exactly one user; 100 only through a historical email.

**Stage 3: Scope.** A case's resources come from:
- **(a) its deletion batch:** deletion-worker activity about 120 minutes before to 180 minutes after `reported_at`;
- **(b) identifier matches:** evidence matching the person's identifiers, captured in `[requested_at, audit_at]`;
- **(c) retained records:** security audit and legal hold records, assigned to the person's nearest case.

Resources attributed only to other people (decoys, look-alikes) or to the same person's other
case are excluded. The hard problem is **shared batches**: several cases with an identical
`reported_at` share batch rows that carry no identifiers. These are assigned by:
- **status binding (learned):** on 252/252 attributed rows, `reported_status` matched the delete row's
  result: COMPLETE with SUCCESS, NOT_STARTED with FAILED, PARTIALLY_COMPLETE with PARTIAL. Used only
  to link rows to cases, never as proof of state;
- **timing:** each row's fit to learned per-row-shape offsets from `reported_at`;
- **ties:** deferred, then broken by each case's own measured schedule delay.

**Stage 4: State.**
- For each resource, the newest evidence captured at or before `audit_at` wins, and SQL copies are
  compared to detect replica drift.
- A snapshot's silence proves absence only for a resource a deletion targeted, and never for
  retained categories.
- Recreation risks:
  - active flows;
  - restorable backups;
  - activity claims ("remains enabled", "can still be restored");
  - sync, index, restore or export activity after treatment.

**Stage 5: Policy.**
- Treatments: DELETE, REDACT, ANONYMIZE, RETAIN, TOMBSTONE (redact all but `user_id`),
  BACKUP (restore must be BLOCKED) and FLOW (must be DISABLED).
- A case is COMPLETE only if every record is compliant, every retained record still exists, and no
  flow or restore path is active.
- A `citable()` guard drops any evidence dated after `audit_at`.
- A record found ABSENT where redaction or anonymization was required is verified as ABSENT.

**Stage 6: Steps.**
- Order: phase, then operation-table order, then `system_id`, then `resource_id`.
- Because of the operation-table order, flows are disabled and restores blocked before records change.
- A downstream copy (for example a search document naming its `source_resource_id`) is moved ahead of its source.
- `step_no` is consecutive from 1, and JSON is serialized with fixed key order. Output rows are
  sorted by numeric `case_id`, and reruns are byte-identical.

## 5. Why deterministic first, and where LLMs are used

Most of the task is joins, timestamp comparisons and rule application. Code does these exactly,
for free and repeatably. An LLM could invent resource ids or citations and would vary between runs.
LLMs are used **only where input is not machine-readable**:

| Use | Model | Guard rails |
|---|---|---|
| Screenshot extraction | `gemini-3.8-flash` (vision) | strict JSON schema, validation, repair retry, normalization, hash cache |
| Intent fallback | `gemini-3.8-flash` (text) | batches of 20, constrained labels, text-hash cache; 0 calls needed in the final run |

Every call and cache hit is logged (provider, model, prompt, run id, tokens) to
`.cache/llm_usage.jsonl`. API keys come only from the environment or a git-ignored `.env`
(`GEMINI_API_KEY`; optional `VISION_PROVIDER=nvidia` plus `VISION_API_KEY` for Nemotron). Keys are
never logged.

## 6. Results

### Public samples (32 cases)

| Metric | Result |
|---|---|
| Case status accuracy | **100%** (32/32: 10 COMPLETE, 22 INCOMPLETE) |
| Step precision | **97.4%** (185 of 190 predicted steps exactly match) |
| Step recall | **95.9%** (185 of 193 gold steps reproduced) |
| Step F1 | **96.6%** |
| Field accuracy (system_id, resource_id, operation, state, evidence_id) | **95.9% each** (185/193) |
| Sequence accuracy (whole `steps_json` identical) | **71.9%** (23/32) |

Sequence accuracy is all-or-nothing per case. The 9 non-exact cases (03, 04, 18, 20, 21, 24, 26,
27, 28) all have the right status and are each off by about one step. No miss is caused by step
order or a wrong citation.

### Evaluation set (518 cases, `output.csv`)

- 151 COMPLETE, 357 INCOMPLETE, 10 NOT_APPLICABLE; 1,205 evidence citations.
- Independent validator (`validate_output.py`): **0 violations**. It checks:
  - header, row count and sort order;
  - valid JSON;
  - allowed phase / operation / state combinations;
  - consecutive `step_no` and ordering;
  - NOT_APPLICABLE rows are `[]`; COMPLETE rows are verification-only with citations;
  - INCOMPLETE remediation and verification steps have empty evidence ids;
  - every cited id exists and is dated at or before `audit_at`.
- 3 warnings: cases 55, 61, 63 are COMPLETE with `[]` (see §9).
- The validator flagged 11 violations on a deliberately corrupted copy, so it does detect errors.
- The real accuracy on the 518 cases is unknown (hidden answers). The samples suggest nearly all
  statuses are right and about 70% of step lists are exact.

## 7. Evaluation work

**Custom cases** (`custom_cases.py`, offline, 18/18 pass), all hand-derived from the problem
statement and rules:

| # | Case | Expected | Result |
|---|---|---|---|
| C1a | "Please don't delete my account. I only need a copy of my order history." | NOT_APPLICABLE | pass |
| C1b | Spanish deletion request | authorizes deletion | pass |
| C1c | Marketing opt-out | NOT_APPLICABLE | pass |
| C1d | German deletion request | authorizes deletion | pass |
| C1e | French correction request | rules defer to the LLM rather than guess | pass, and exposes a gap |
| C2a | Historical email, upper-cased | resolves to the right user | pass |
| C2b | Old email hash | kept as an identifier | pass |
| C2c | Unknown email | not_found | pass |
| C3a | Proof of a remaining record captured after `audit_at` | no finding | pass |
| C3b | Same proof captured before `audit_at` | finding with citation | pass |
| C4a | Tombstone still showing name/email | REPORT_EXPOSED_PII + REDACT_RECORD, never delete | pass |
| C4b | Tombstone correctly redacted | VERIFY_RECORD_REDACTED | pass |
| C4c | Invoice under a RETAIN rule | VERIFY_RECORD_PRESENT, no remediation | pass |
| C4d | Retained invoice missing | INCOMPLETE | pass |
| C5a-d | Active sync + restorable backup + profile + its search copy | disable flow → block restore → delete copy → delete source; phases ordered; empty future evidence ids; valid JSON | pass |

**Other checks:**
- the intent LLM fallback got 5/5 synthetic ambiguous texts right (734 input / 362 output tokens);
- 53/53 deterministic extraction checks pass;
- 41/41 screenshot fields are correct with Gemini.

## 8. Vision model comparison

All 11 screenshots were hand-transcribed into 41 identifier field checks, and each model ran with the same prompt.

| Model | Correct | Calls | Input / output tokens | Median time per image | Outcome |
|---|---|---|---|---|---|
| `gemini-3.8-flash` (Google) | **41/41** | 12 | ~24.6k / ~6.7k | ~187 s | **chosen default** |
| `nvidia/nemotron-3.5-super-vl-120b-a12b`, run 1 | 36/41, then 40/41 after post-processing fixes | 14 | ~28.6k / ~45.6k | ~60 s | kept as fallback |
| Nemotron, run 2 (tighter prompt) | 39/41 | 13 | ~30.8k / ~51.7k | ~60 s | |
| `meta/llama-3.2-90b-vision-instruct` | n/a | 3 | 0 | timed out (>180 s) | rejected |
| Gemini Pro models (pro-latest, 3.1-pro-preview, 4-argon) | n/a | n/a | n/a | n/a | 429 quota errors on the free key |

Nemotron's misses were single-character OCR slips on non-subject text: the decoy email
`g.silva76.jr@gmail.com` read as `g.silva76jr@gmail.com`, and "Kofi Agyeman" read as "Kofi Agyman".
Nemotron spends most of its output on reasoning tokens. Gemini was more accurate and much cheaper
in output tokens, but slower.

## 9. Iterations, failures and limitations

**Score progression on the samples** (each change is a general rule, never a case-specific exception):

| Version | Step P / R / F1 | Sequence | Decision |
|---|---|---|---|
| Baseline (stages 1-6) | 96.3 / 94.8 / 95.6 | 65.6% | |
| Drop tie-broken batch rows | — / — / 95.2 | 62.5% | rejected |
| Iteration 1: per-case schedule shift | 95.2 / 93.3 / 94.2 | 62.5% | reverted: fixed case 26 but broke 19, 20, 21, 25 |
| Iteration 2: status binding for batch rows | 97.4 / 95.3 / 96.3 | 68.8% | kept: fixed case 22. A first version keyed on action + system broke cases 06 and 08, so the key was narrowed to exact row wording. |
| Iteration 3: defer ties, break them by each case's own schedule | **97.4 / 95.9 / 96.6** | **71.9%** | kept: fixed 09 and 23; 20 and 24 each gained one wrong row |
| Iteration 4 idea: prefer claimants that ran other actions | — | — | rejected before coding: would move case 25's record to 26 |

**Fixed during development:**
- evidence dated after `audit_at` being cited (fixed with the `citable()` guard);
- treating workflow claims as proof;
- detecting replica drift;
- screenshot OCR errors;
- `decision=deny` not mapped to BLOCKED;
- partial snapshots falsely proving a retained audit record absent;
- batch windows wide enough to let cases claim neighbours' rows.

**Remaining sample misses (8 gold steps):**
- 3 resources appear in no participant file or activity: `account_004`, `audit_record_6645`,
  `audit_record_5569`. Producing them would mean guessing.
- 5 are identifier-free anonymized analytics rows tied between cases sharing a `reported_at`; after
  anonymization nothing links them to a person.

**Known limitations:**
- Non-English *non-deletion* requests (C1e) are not covered by the rules. They go to the LLM;
  without a key they stay unresolved and are processed as deletions. This does not affect the
  submitted output.
- Without `GEMINI_API_KEY` the 11 screenshots are skipped, and 8 evaluation cases (34, 35, 39, 45,
  46, 50, 52, 53) lose screenshot-only findings. The submitted output was produced with screenshots.
- **Found after submission**, by reviewing the 12 planted `LINK_RESOURCE_IDENTITY` cases (9 are
  handled correctly):
  - **Case 55** is probably wrong. Screenshot `evidence_32` shows `backup_9287` as a healthy,
    restorable archive with no restore block. The policy only flags backups in state ACTIVE, not
    PRESENT, so a REPORT_RESTORE_RISK was likely missed.
  - **Case 61** is probably wrong. Screenshot `evidence_46` shows the attachment `object_19`
    (`delivery_confirmation_19.pdf`) still stored, so a REPORT_REMAINING_RECORD was likely missed.
  - **Case 63** is likely correct as COMPLETE. Screenshot `evidence_60` shows `marketing_001`
    belongs to a different person (Soo-jin Park), so the catalog link is a decoy.
  - These were not fixed, because the screenshot cache and API key were on the participant's laptop.

## 10. Token usage and cost (final run)

- The final run that wrote `output.csv` made **0 new model calls**: all 11 screenshot results came
  from the cache, and the rules decided all intents.
- The cache was built by **12 `gemini-3.8-flash` calls: ~24,600 input + ~6,700 output = ~31,300
  tokens**. That is about 60.4 tokens per request (47.5 in, 12.9 out) over 518 requests, and about
  2,850 tokens per screenshot.
- **Actual cost: $0** (Gemini free tier).
- Paid-tier equivalent, from third-party price listings:
  - $0.75 / $3.75 per 1M tokens (introductory rate to 2026-12-31): **$0.0436 total, $0.000084 per request**;
  - $1.50 / $7.50 per 1M tokens (from 2027): **$0.0872 total, $0.000168 per request**.
- `pdfplumber` (local) and the unused Nemotron fallback cost $0.
- Time: about 28 minutes for the vision calls on the free tier. With the cache warm, the full run takes 1-2 seconds.
- Token figures are rounded (to hundreds), copied from the run summaries in `log.txt`, because the
  raw usage log is git-ignored.

## 11. The participant's decisions and process

The participant directed an AI coding agent (Claude Code, logging to `log.txt` as required).
Decisions they made, visible in the log:

1. **Analysis before code.** First mapped the data and how case → email → user_id → historical
   identifiers → evidence → rules link together. Then had all 32 sample answers reverse-engineered
   into general rules (`rules_derived.md`) before writing solution code.
2. **Free models only.** Explicitly chose free API keys over paid Claude API keys.
3. **Vision model selection.**
   - Tried NVIDIA first; Llama 3.2 Vision timed out.
   - Switched to Nemotron with larger token limits and checked every value against the images.
   - Then asked for a field-by-field Gemini comparison.
   - Chose Gemini as the default and kept Nemotron as a fallback.
4. **Deterministic first.** Specified rules-first intent detection with a batched, cached LLM
   fallback, and asked for prompts in `code/prompts/`, caching, and token logging for every call.
5. **Staged build.** Specified stages 1-2, 3-4 and 5-6 separately, each with explicit requirements
   and a check on the samples.
6. **Never special-case.** The improvement loop: fix the largest failure group with a general rule,
   record before/after scores in `iterations.md`, and stop at the plateau.
7. **Verification.** Asked for a clean-cache run, an independent output validator, secret scans, and
   packaging checks before submitting.

## 12. Repository and submission facts

- GitHub: `Sohan258oss/hackerrank-orchestrate-october26`, branch `main-y99994`. The work was
  started locally (Windows) and finished in a Claude Code cloud session.
- `code.zip`: built with the organizers' exact command
  (`zip -r code.zip README.md code -x "*/__pycache__/*" ...`). 40 files, 95 KB. It contains no
  dataset, `.env`, nested archives, caches or secrets, and includes both reports. It runs on its own
  when unpacked next to `dataset/`.
- `log.txt`: 31 well-formed entries, each with `tool=Claude Code` and `model=claude-opus-5-5`.
  Weaknesses: many admin and git-retry entries in the later session, one out-of-order timestamp
  (corrected in a later entry), and some summaries longer than the 2-5 sentences the format asks for.
- Submission page:
  https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

## 13. Quick answers to likely questions

- **Biggest technical challenge?** Dividing shared deletion batches between cases with identical
  `reported_at` when the rows carry no identifiers. Solved mostly by learned status binding and
  per-case timing.
- **Why not trust `reported_status`?** The task says it is a claim, and the evidence often
  contradicts it. It is used only to link batch rows to cases, never as proof of state.
- **How is the `audit_at` cutoff enforced?** Through the `citable()` guard, and checked again by the
  validator (1,205/1,205 citations are on time).
- **How is the output made reproducible?** Deterministic stages, hash caches, fixed JSON key order,
  sorted rows. Reruns are byte-identical.
- **What would you do next?**
  1. Treat backups with no restore block as a restore risk.
  2. Debug the missed attachment in case 61.
  3. Add non-English non-deletion rules.
  4. Use in-window catalog links for scoping.
  5. Always regenerate the output with the screenshot cache present.
