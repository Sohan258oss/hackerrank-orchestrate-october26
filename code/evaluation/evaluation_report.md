# Evaluation report

All numbers come from participant-facing data only: the 32 public samples
(`dataset/sample_deletion_requests.csv` scored against `dataset/sample_output.csv`), the
participant-made cases in `code/evaluation/custom_cases.py`, and hand checks of the screenshot
evidence. No organizer-only files were used.

Reproduce:

```bash
python code/evaluation/main.py              # predicts the 32 samples, then scores them
python code/evaluation/custom_cases.py      # participant-made cases (offline, no model calls)
python code/evaluation/validate_output.py   # independent contract check of ./output.csv
```

## 1. Accuracy on the public samples

### 1.1 Case-level status accuracy

| Gold status | Cases | Predicted correctly |
|---|---|---|
| COMPLETE | 10 | 10 |
| INCOMPLETE | 22 | 22 |
| **All** | **32** | **32 (100.0%)** |

### 1.2 Step-level accuracy

A predicted step counts as correct only when all six compared fields match a gold step: phase,
system_id, resource_id, operation, state and evidence_id.

| Metric | Count | Score |
|---|---|---|
| Gold steps | 193 | |
| Predicted steps | 190 | |
| Exactly matching steps | 185 | |
| Step precision | 185 / 190 | **97.4%** |
| Step recall | 185 / 193 | **95.9%** |
| Step F1 | | **96.6%** |

### 1.3 Field-level accuracy

Each gold step is paired with the predicted step for the same phase and resource. A field is
correct when the pair exists and the field value matches.

| Field | Correct / gold steps | Accuracy |
|---|---|---|
| system_id | 185 / 193 | 95.9% |
| resource_id | 185 / 193 | 95.9% |
| operation | 185 / 193 | 95.9% |
| state | 185 / 193 | 95.9% |
| evidence_id | 185 / 193 | 95.9% |

All five fields score the same because no matched step has a wrong system, operation, state or
citation. The 8 misses are gold steps for resources the agent did not assign to the case (§4.3).

### 1.4 Sequence accuracy

| Metric | Score |
|---|---|
| Cases whose entire `steps_json` array (order and step numbers included) equals gold | **23 / 32 (71.9%)** |

| Cases | Result |
|---|---|
| 01, 02, 05-17, 19, 22, 23, 25, 29-32 | exact match |
| 03, 26, 27, 28 | anonymized analytics record swapped with another case that has the same `reported_at` |
| 04, 18 | one gold resource appears in no evidence file and no activity row (unreachable) |
| 20 | one unreachable audit record, plus one extra tied analytics record |
| 21 | analytics record lost to a tied case |
| 24 | one extra tied analytics record |

No mismatch is caused by step ordering. Every case with the right set of steps is also in the right order.

### 1.5 Full evaluation set (518 cases)

The submitted `output.csv` has 151 COMPLETE, 357 INCOMPLETE and 10 NOT_APPLICABLE cases, with 1,205
evidence citations. `validate_output.py` checks it independently of the pipeline:

| Check | Result |
|---|---|
| Exact header, 518 rows, one per request, sorted by `case_id` | pass |
| Every `steps_json` is a valid JSON array with exactly the 7 step fields | pass |
| Only allowed phase / operation / state combinations | pass |
| `step_no` consecutive from 1 | pass |
| Phase order, then operation order, then system_id, resource_id | pass |
| NOT_APPLICABLE rows are `[]` | pass (10/10) |
| COMPLETE rows contain only VERIFICATION steps, each citing evidence | pass |
| INCOMPLETE findings cite evidence; remediation and verification use `""` | pass |
| Every cited id exists and was captured or logged at or before `audit_at` | pass (1,205/1,205) |
| **Violations** | **0** |
| Warnings | 3 (cases 55, 61, 63: COMPLETE with `[]`, because their only resources have no state evidence at or before `audit_at`) |

The validator reports 11 violations on a deliberately corrupted copy, so it does detect errors. Reruns
of `code/main.py` produce byte-identical output.

## 2. Participant-made evaluation cases

The public samples contain no NOT_APPLICABLE case and few tombstone and ordering cases, so
`code/evaluation/custom_cases.py` adds hand-built scenarios. Their expected answers were derived by
hand from `problem_statement.md` and `deletion_rules.csv`.

**Result: 18 / 18 checks pass.**

| # | Case | Input | Expected (hand-derived) | Result |
|---|---|---|---|---|
| C1a | Negated deletion plus copy request | "Please don't delete my account. I only need a copy of my order history." | not a deletion -> NOT_APPLICABLE | pass |
| C1b | Spanish deletion | "Por favor, eliminen mi cuenta y todos mis datos personales." | authorizes deletion | pass |
| C1c | Marketing opt-out | "Stop sending me marketing emails, unsubscribe me from the newsletter." | NOT_APPLICABLE | pass |
| C1d | German deletion | "Bitte löschen Sie mein Konto und alle meine Daten." | authorizes deletion | pass |
| C1e | French correction | "Pouvez-vous corriger mon adresse e-mail dans mon profil ?" | rules defer to the LLM instead of guessing | pass (see §4.4) |
| C2a | Request made with a historical email, upper-cased | old email from `user_audit_log.csv` | resolves to the same `user_id` | pass |
| C2b | Old email hash | sha256 of the old email | kept among the identifiers used to match evidence | pass |
| C2c | Unknown email | `nobody@example.invalid` | `not_found` | pass |
| C3a | Only proof of a remaining profile captured after `audit_at` | evidence 1 day after cutoff | no finding, no citation | pass |
| C3b | Same proof captured before `audit_at` | evidence 1 month before cutoff | `REPORT_REMAINING_RECORD` citing it | pass |
| C4a | Account tombstone still showing name/email | `ACCOUNT_TOMBSTONE`, PRESENT | `REPORT_EXPOSED_PII` + `REDACT_RECORD` (never delete) | pass |
| C4b | Tombstone correctly redacted | REDACTED | `VERIFY_RECORD_REDACTED` with evidence | pass |
| C4c | Invoice under a RETAIN rule | PRESENT | `VERIFY_RECORD_PRESENT`, no remediation | pass |
| C4d | Retained invoice missing | ABSENT | case INCOMPLETE | pass |
| C5a | Active sync + restorable backup + profile + search copy of the profile | 4 violating resources | remediation order: disable flow, block restore, delete search copy, delete profile | pass |
| C5b | Same | | phases FINDING -> REMEDIATION -> VERIFICATION, steps numbered from 1 | pass |
| C5c | Same | | remediation/verification `evidence_id` is `""` | pass |
| C5d | Same | | `steps_json` is valid JSON | pass |

Other component checks:

| Component | Check | Result |
|---|---|---|
| Intent LLM fallback (`gemini-3.8-flash`) | 5 synthetic texts with conflicting or missing signals, one batched call (734 input / 362 output tokens) | 5 / 5 correct; rerun served from cache |
| Intent rules on the 32 samples | deletion vs. not | 32 / 32 |
| Intent rules on the 518 evaluation texts | non-deletion requests found | 10, matching the 10 `CLASSIFY_REQUEST` activities |
| Identity resolution | evaluation requests resolved to exactly one user | 518 / 518 (100 through a historical email) |
| Non-image extraction | deterministic checks against facts confirmed in the samples | 53 / 53 |

## 3. Vision model comparison (screenshot extraction)

The 11 application screenshots are the only evidence that needs a vision model. Every identifier
(emails, phones, names, user ids, resource ids) in all 11 was transcribed by hand, giving
41 field checks. Each model ran with the same strict-JSON prompt (`code/prompts/image_extraction.md`).

| Model (provider) | Fields correct | Calls | Input tokens | Output tokens | Median time / image | Outcome |
|---|---|---|---|---|---|---|
| `gemini-3.8-flash` (Google) | **41 / 41** | 12 | ~24,600 | ~6,700 | ~187 s | **chosen as default** |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` (NVIDIA), run 1 | 36 / 41, then 40 / 41 after post-processing fixes | 14 | ~28,600 | ~45,600 | ~60 s | fallback (`VISION_PROVIDER=nvidia`) |
| `nvidia/nemotron-3.5-super-vl-120b-a12b`, run 2 (tighter prompt) | 39 / 41 | 13 | ~30,800 | ~51,700 | ~60 s | |
| `meta/llama-3.2-90b-vision-instruct` (NVIDIA) | n/a | 3 | 0 | 0 | timed out (>180 s) | rejected |
| `gemini-pro-latest`, `gemini-3.1-pro-preview`, `gemini-4-argon` | n/a | | | | 429 quota exceeded on free key | not usable |

Nemotron's misses were single-character OCR errors on non-subject text. It read the decoy email
`g.silva76.jr@gmail.com` as `g.silva76jr@gmail.com` in both runs, and "Kofi Agyeman" as "Kofi Agyman" in
run 2. All subject emails, phones, user ids and resource ids were correct in both runs. Gemini read
everything exactly. It used somewhat fewer input tokens and about an eighth of the output tokens, because Nemotron
spends most of its output on reasoning. Gemini was slower per image. Both models found the link from
`account_5762` to `support_9538`.

## 4. Failures found and improvements made

The full iteration log is in `code/evaluation/iterations.md`. Every change is a general rule in one
pipeline stage, never a case-specific exception.

### 4.1 Score progression on the samples

| Version | Status | Step P / R / F1 | Sequence | Decision |
|---|---|---|---|---|
| Baseline (stages 1-6) | 100% | 96.3 / 94.8 / 95.6 | 65.6% (21/32) | |
| Variant: drop tie-broken batch rows | 100% | — / — / 95.2 | 62.5% (20/32) | rejected |
| Iteration 1: per-case schedule shift for anonymous batch rows | 100% | 95.2 / 93.3 / 94.2 | 62.5% (20/32) | reverted |
| Iteration 2: `reported_status` must agree with a status-bound batch row | 100% | 97.4 / 95.3 / 96.3 | 68.8% (22/32) | accepted |
| Iteration 3: break remaining ties by each case's own schedule | 100% | **97.4 / 95.9 / 96.6** | **71.9% (23/32)** | accepted |

- **Iteration 1 (reverted).** Cases 03, 09, 26, 27 and 28 lost anonymized analytics rows to other cases
  sharing `reported_at`, because every case in a batch has the same global offset. Estimating a
  per-case start delay fixed case 26. But the two-round assignment moved other rows and cases 19, 20, 21
  and 25 regressed: F1 fell from 95.6 to 94.2 and sequence from 65.6% to 62.5%.
- **Iteration 2 (accepted).** Cases 21, 22 and 23 were given the wrong identifier-free primary-profile
  delete. On all 252 rows that evidence attributes to a person, `reported_status` matches the delete
  row's result: COMPLETE with SUCCESS, NOT_STARTED with FAILED, PARTIALLY_COMPLETE with PARTIAL. The
  binding is learned per exact row wording (at least 30 rows, one result per status), not hard-coded. A
  first version keyed only on action and system wrongly bound older wording and broke cases 06 and 08,
  so the key was narrowed. `reported_status` is used only to link rows to cases, never as proof of
  state. Case 22 was fixed.
- **Iteration 3 (accepted).** Ties are now deferred until all clear-cut rows are placed. Offsets are
  then relearned, and each claimant's delay is measured from its own tightly timed rows (shapes with a
  spread of at most 5 minutes, seen at least 5 times). Cases 09 and 23 were fixed; cases 20 and 24 each
  gained one wrong row, for a net gain of one exact case.
- **Plateau.** A fourth rule (prefer tie claimants that ran other policy actions) was checked against
  the data before coding and rejected, because it would move case 25's record to case 26. Anonymize rows
  have only 28 clear-cut placements to learn from against 96 tie-broken ones, so the timing signal is
  exhausted.

### 4.2 Failures fixed during development

- **Evidence after `audit_at` leaking into answers.** A `citable()` guard now drops any file captured,
  or activity logged, after the cutoff.
- **Workflow claims taken as proof.** States come from snapshots, exports and logs. An activity
  `SUCCESS` counts only as a claim. A flow or restore path without an observation is treated as
  active only when an activity check before `audit_at` reports it open.
- **Replica drift.** SQL replicas are loaded as separate in-memory SQLite databases, so a record deleted on
  the primary but still present on a regional, reporting or failover replica is reported.
- **Screenshot OCR errors.** Resource ids written inside fields are promoted, linked ids are recorded,
  and only DATA_FLOW/BACKUP_ARCHIVE may be ACTIVE. Cached model output is re-validated on load. This took
  Nemotron from 36/41 to 40/41 without new calls.
- **Restore authorization `decision=deny`** was not mapped to BLOCKED; fixed in the extractor.

### 4.3 Remaining failures (not fixed)

| Failure | Cases | Missed gold steps | Why it remains |
|---|---|---|---|
| Gold resource never appears in participant data | 04 (`account_004`), 18 (`audit_record_6645`), 20 (`audit_record_5569`) | 3 | Producing these would mean guessing ids. |
| Identifier-free `ANONYMIZE_RECORD` rows tied between cases with the same `reported_at` | 03, 20, 21, 24, 26, 27, 28 | 5 | After anonymization no evidence links the row to a person, and timing cannot separate claimants without tightly timed rows of their own. |

### 4.4 Known limitations

- **Non-English non-deletion requests** (C1e) are not matched by the rules and are deferred to the
  LLM. Without an API key an undecided text stays unresolved and is processed as a deletion. All 518
  evaluation texts are decided by the rules, so `output.csv` does not depend on this.
- **Screenshots need a vision key.** Without `GEMINI_API_KEY` the 11 screenshots are skipped. Sample
  scores are unchanged, but 8 evaluation cases (34, 35, 39, 45, 46, 50, 52, 53) then lose findings
  that only a screenshot proves. The submitted `output.csv` was produced with the screenshots extracted.
- **Resources with no state evidence before `audit_at`** (cases 55, 61, 63) produce COMPLETE with
  no verification steps rather than an uncited guess.
