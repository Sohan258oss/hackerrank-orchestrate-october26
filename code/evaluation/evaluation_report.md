# Evaluation report

All numbers come from participant-facing data only: the 32 public samples
(`dataset/sample_deletion_requests.csv` scored against `dataset/sample_output.csv`) and the
participant-made cases in `code/evaluation/custom_cases.py`.

Reproduce:

```bash
python code/evaluation/main.py            # predicts the 32 samples, then scores them
python code/evaluation/custom_cases.py    # participant-made cases (offline, no model calls)
```

## 1. Metrics on the public samples

Final pipeline, run from the repository root:

| Metric | Definition | Score |
|---|---|---|
| Case-level status accuracy | `deletion_status` equal to gold | **100.0%** (32/32) |
| Step precision | predicted steps that match a gold step exactly (phase, system, resource, operation, state, evidence id) | **97.4%** |
| Step recall | gold steps reproduced exactly | **95.9%** |
| Step F1 | | **96.6%** |
| Sequence accuracy | whole `steps_json` array identical to gold, order and step numbers included | **71.9%** (23/32) |

Field-level accuracy (gold steps matched to a predicted step by phase + resource):

| system_id | resource_id | operation | state | evidence_id |
|---|---|---|---|---|
| 95.9% | 95.9% | 95.9% | 95.9% | 95.9% |

All five fields score the same: every predicted step for a resource the agent found carries the
right system, operation, state and evidence. Every field error comes from a resource that is
missing or assigned to the wrong case (see §3), not from a wrong operation or citation.

Per-case results:

| Cases | Result |
|---|---|
| 01, 02, 05-17, 19, 22, 23, 25, 29-32 | exact match |
| 03, 26, 27, 28 | an anonymized analytics record swapped with another case that has the same `reported_at` |
| 04, 18 | a gold resource that appears in no evidence file and no activity row (unreachable) |
| 20 | one unreachable audit record plus one extra tied analytics record |
| 21 | analytics record lost to a tied case |
| 24 | one extra tied analytics record |

Full evaluation set (518 cases, `output.csv`): 151 COMPLETE, 357 INCOMPLETE, 10 NOT_APPLICABLE;
all contract checks in `code/main.py` pass (one row per case, sorted, consecutive step numbers,
phase order, evidence ids only where allowed). Each of the 1,201 citations was captured or logged at
or before its case's `audit_at`. Reruns are byte-identical.

## 2. Participant-made evaluation cases

The public samples contain no NOT_APPLICABLE case and few tombstone and ordering cases, so
`code/evaluation/custom_cases.py` adds hand-built scenarios. Their expected answers were derived
by hand from `problem_statement.md` and `deletion_rules.csv`. Result: **18/18 checks pass**.

| # | Case | Input | Expected (hand-derived) | Result |
|---|---|---|---|---|
| C1a | Negated deletion plus copy request | "Please don't delete my account. I only need a copy of my order history." | not a deletion -> NOT_APPLICABLE | pass |
| C1b | Spanish deletion | "Por favor, eliminen mi cuenta y todos mis datos personales." | authorizes deletion | pass |
| C1c | Marketing opt-out | "Stop sending me marketing emails, unsubscribe me from the newsletter." | NOT_APPLICABLE | pass |
| C1d | German deletion | "Bitte löschen Sie mein Konto und alle meine Daten." | authorizes deletion | pass |
| C1e | French correction | "Pouvez-vous corriger mon adresse e-mail dans mon profil ?" | rules defer to the LLM instead of guessing | pass (see §3.4) |
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

The LLM intent fallback was also tested on 5 synthetic texts that the rules could not decide
(conflicting or missing signals): Gemini
(`gemini-3.8-flash`) labelled all 5 correctly in one batched call. A rerun was served from the cache.

The vision extractor was checked separately against a hand transcription of all 11 screenshots
(41 identifier fields): `gemini-3.8-flash` 41/41, `nvidia/nemotron-3.5-super-vl-120b-a12b` 39/41
(two single-character OCR slips). Gemini was made the default. On the non-image evidence, 53
deterministic extraction checks against facts confirmed in the samples pass.

## 3. Failures found and improvements made

The full iteration log, with before/after scores, is in `code/evaluation/iterations.md`. Every change
is a general rule in one pipeline stage, never a case-specific exception.

### 3.1 Score progression

| Version | Status | Step P / R / F1 | Sequence |
|---|---|---|---|
| Baseline (stages 1-6) | 100% | 96.3 / 94.8 / 95.6 | 65.6% |
| Variant: drop tied batch rows | 100% | — / — / 95.2 | 62.5% (rejected) |
| Iteration 1: per-case schedule shift | 100% | 95.2 / 93.3 / 94.2 | 62.5% (reverted) |
| Iteration 2: `reported_status` must agree with a status-bound batch row | 100% | 97.4 / 95.3 / 96.3 | 68.8% |
| Iteration 3: break remaining ties by each case's own schedule | 100% | **97.4 / 95.9 / 96.6** | **71.9%** |

### 3.2 Failures fixed during development

- **Evidence after `audit_at` leaking into answers.** A `citable()` guard now drops any file captured,
  or activity logged, after the cutoff. Later evidence is kept only as a debugging note.
- **Workflow claims taken as proof.** States come from snapshots, exports and logs. An activity
  `SUCCESS` counts only as a claim. A flow or restore path without an observation is treated as
  active only when an activity check before `audit_at` reports it open.
- **Replica drift.** SQL replicas are loaded as separate in-memory SQLite databases. A record deleted on
  the primary but still present on a regional, reporting or failover replica is reported.
- **Screenshot OCR errors.** Resource ids written inside fields are promoted, linked ids are recorded,
  and only DATA_FLOW/BACKUP_ARCHIVE may be ACTIVE. Cached model output is re-validated on load. This took
  Nemotron from 36/41 to 40/41 before Gemini replaced it.
- **Restore authorization `decision=deny`** was not mapped to BLOCKED; fixed in the extractor.
- **Shared deletion batches** (several cases with identical `reported_at` and identifier-free rows):
  iterations 2 and 3 above.

### 3.3 Remaining failures (not fixed)

| Failure | Cases | Why it remains |
|---|---|---|
| Gold resource never appears in participant data | 04 (`account_004`), 18 (`audit_record_6645`), 20 (`audit_record_5569`) | Producing these would mean guessing ids. |
| Identifier-free `ANONYMIZE_RECORD` rows tied between cases with the same `reported_at` | 03, 20, 21, 24, 26, 27, 28 | After anonymization no evidence links the row to a person, and timing cannot separate claimants that have no tightly timed rows of their own. A further rule (prefer claimants that ran other policy actions) was tested against the data and rejected because it would break case 25. |

### 3.4 Known limitations

- **Non-English non-deletion requests** (C1e) are not matched by the rules. They are deferred to the
  LLM. Without an API key an undecided text stays unresolved and is processed as a deletion. All 518
  evaluation texts are decided by the rules (the 10 NOT_APPLICABLE cases match the 10
  `CLASSIFY_REQUEST` activities), so `output.csv` does not depend on this.
- **Screenshots need a vision key.** Without `GEMINI_API_KEY` the 11 screenshots are skipped. On the
  samples the scores are unchanged, but 8 evaluation cases (34, 35, 39, 45, 46, 50, 52, 53) then lose
  findings that only a screenshot proves. The submitted `output.csv` was produced with the
  screenshots extracted.
