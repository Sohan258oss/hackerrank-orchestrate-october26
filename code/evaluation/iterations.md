# Improvement iterations

Each iteration fixes the largest remaining failure group on the 32 public samples by changing a
general rule in one pipeline stage (never a case-specific exception), then re-runs
`python code/evaluation/main.py`.

Metrics: status accuracy; step precision / recall / F1 (steps compared as whole tuples);
sequence accuracy (whole `steps_json` array equal to gold).

## Baseline (after stages 1-6)

| status | step P | step R | step F1 | sequence |
|---|---|---|---|---|
| 100.0% | 96.3% | 94.8% | 95.6% | 65.6% (21/32) |

Imperfect cases by root cause:

| Group | Cases | Stage |
|---|---|---|
| Shared-batch tie: anonymized analytics record given to the wrong case | 03, 09, 26, 27, 28 | 3 (scoping) |
| Shared-batch tie: deleted primary profile given to the wrong case | 21, 22, 23 | 3 (scoping) |
| Gold resource appears in no evidence file and no activity row | 04 (`account_004`), 18 (`audit_record_6645`), 20 (`audit_record_5569`) | none: unreachable |

## Iteration 1 — per-case schedule shift for anonymous batch rows (rejected)

- **Failure group:** shared-batch ties on anonymized analytics records (cases 03, 09, 26, 27, 28): two
  or more cases with the same `reported_at` can claim the same identifier-free `ANONYMIZE_RECORD` row.
- **Root cause (stage 3):** the assignment scored a row only against its row shape's global offset from
  `reported_at`, which is identical for cases sharing `reported_at`; the tie then fell to batch order.
- **Fix tried:** each case's completed policy actions run on a tight shared schedule (for example
  block-restore to disable-flow: median 449 s apart, 38 s spread), so a per-case start shift was
  estimated from the case's own tightly timed rows, unique claimants were assigned first, offsets were
  relearned, and contested rows were scored against `learned offset + case shift` (with the row shape's
  spread added when a case had no measured shift).
- **Scores:** before 96.3 / 94.8 / F1 95.6, sequence 65.6% → after 95.2 / 93.3 / F1 94.2, sequence 62.5%.
- **Decision:** reverted. It fixed case 26 but the two-round assignment moved other rows (cases 19, 20,
  21, 25 regressed); cases with no tightly timed rows of their own have no measurable shift, so the
  signal is too weak to beat the single-pass rule on these samples.

## Iteration 2 — reported_status must agree with a status-bound batch row (accepted)

- **Failure group:** shared-batch ties on deleted primary profiles (cases 21, 22, 23): the wrong
  identifier-free `account_*` delete row was given to the case.
- **Root cause (stage 3):** assignment ignored the one field that differs between cases sharing a batch:
  the workflow's `reported_status` is the summary of the case's own primary-profile delete. On all 252
  rows that evidence attributes to a person, `COMPLETE` ⇔ `SUCCESS`, `NOT_STARTED` ⇔ `FAILED`,
  `PARTIALLY_COMPLETE` ⇔ `PARTIAL` with no exception.
- **Fix:** learn, per row kind (action plus exact wording, ids masked), whether `reported_status`
  determines `reported_result` on attributed rows (at least 30 rows, one result per status). Where it
  does, a contested row is only offered to cases whose status agrees with the row's result. The
  binding is learned, not hard-coded: only the primary-profile wording qualifies in this dataset; a
  first version keyed on action + target system wrongly bound the hand-written early-case wording and
  broke cases 06 and 08, so the key was narrowed to the exact wording. `reported_status` is used only
  for linkage, never as proof of state.
- **Scores:** before 96.3 / 94.8 / F1 95.6, sequence 65.6% → after 97.4 / 95.3 / F1 96.3, sequence 68.8% (22/32).
- **Effect:** case 22 fixed; case 21 no longer takes a wrong account (its own `account_6440` is still
  unassigned); case 23 still picks the wrong one of several `FAILED` rows shared with evaluation cases.

## Iteration 3 — break remaining ties by each case's own schedule (accepted)

- **Failure group:** shared-batch ties on anonymized analytics records (cases 03, 09, 26, 27, 28) plus the
  remaining primary-profile tie (case 23).
- **Root cause (stage 3):** genuine ties were broken by batch order, which carries no ownership signal.
- **Fix:** the single-pass assignment is unchanged for every row it decides clearly; rows that end in a
  tie are deferred. After all clear-cut rows are placed, offsets are relearned per exact row shape, each
  claimant's start delay is measured from its own tightly timed rows (shapes with a spread of at most
  5 minutes, seen at least 5 times), and the tie goes to the claimant whose schedule
  (`learned offset + own delay`) the row fits best; a claimant without a measured delay gets the row
  shape's spread added. Unlike iteration 1, nothing outside the ties can move.
- **Scores:** before 97.4 / 95.3 / F1 96.3, sequence 68.8% → after 97.4 / 95.9 / F1 96.6, sequence 71.9% (23/32).
- **Effect:** cases 09 and 23 fixed; cases 20 and 24 each gained one wrong analytics row from a tie
  that was previously (luckily) resolved the other way; net +1 exact case.

## Plateau and remaining mismatches

A further iteration was evaluated against the data before coding and rejected: preferring tie
claimants that ran other policy actions would move case 25's analytics record (its owner ran none) to
case 26. Anonymize rows have only 28 clear-cut placements to learn their offset from against 96
tie-broken ones, and many claimants have no tightly timed rows of their own, so the schedule signal
is exhausted. Scores have plateaued at status 100%, step F1 96.6%, sequence 71.9%.

| Case | Mismatch | Why it remains |
|---|---|---|
| 04 | `account_004` missing | appears in no evidence file and no activity row (gold proves absence from a snapshot that never names it); unreachable without guessing ids |
| 18 | `audit_record_6645` missing | appears nowhere in the participant data |
| 20 | `audit_record_5569` missing; `analytics_2265` extra | audit record appears nowhere; the analytics row is a tie with an evaluation case sharing `reported_at` |
| 03, 26, 27, 28, 24 | anonymized analytics record swapped or misassigned | identifier-free `ANONYMIZE_RECORD` rows from cases with identical `reported_at`; after anonymization no evidence links them to anyone, and schedule timing cannot separate claimants without measured delays |
| 21 | `analytics_5335` missing | same tie: the row went to another case sharing the batch |
