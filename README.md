# Ghost Data Still Exists: deletion-audit agent

HackerRank Orchestrate, October 2026. For every request in `dataset/deletion_requests.csv`, the
agent decides whether the person actually asked for deletion. For each authorized deletion it then
reconstructs the person's current and historical identifiers, finds every copy of their data in the
evidence, and applies `deletion_rules.csv` to each record as of the case's `audit_at`. It writes
`COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with ordered FINDING, REMEDIATION and VERIFICATION
steps to `output.csv`.

The challenge contract is in [`problem_statement.md`](./problem_statement.md).

## Setup

Requires Python 3.10+ (tested on 3.13).

```bash
pip install -r code/requirements.txt      # pdfplumber, Pillow, requests
cp .env.example .env                      # then set GEMINI_API_KEY=... (free key: https://aistudio.google.com/apikey)
```

The key is read only from the environment or the git-ignored `.env`, and is never logged. Only the
vision step needs it (11 application screenshots). Everything else is parsed locally. To use NVIDIA
Nemotron instead, set `VISION_PROVIDER=nvidia` and `VISION_API_KEY`; see `.env.example`.

## Run

From the repository root:

```bash
python code/main.py                       # all 518 cases -> ./output.csv (prints contract checks and model usage)
python code/evaluation/main.py            # predict + score the 32 public samples
python code/evaluation/custom_cases.py    # participant-made evaluation cases (offline)
python code/evaluation/validate_output.py # independent contract check of ./output.csv
```

Options for `code/main.py`:

| Flag | Effect |
|---|---|
| `--skip-extract` | reuse `.cache/extract/records.jsonl` from a previous run |
| `--no-llm` | intent rules only, no intent-fallback calls |
| `--set sample --out FILE` | run the 32 sample requests instead |
| `--debug FILE` | write per-case decisions and notes as JSONL |

The first run with a key extracts the screenshots with `gemini-3.8-flash`, which takes about 25-30
minutes on the free tier. Results are cached by file hash in `.cache/`, so later runs finish in seconds and
make no model calls. Without a key the screenshots are skipped. The run still completes and passes
the contract checks, but 8 evaluation cases then lose findings that only a screenshot proves.

## How it works

```text
0 extract   94 evidence files -> 2,936 normalized records {file_id, captured_at, system_id,
            resource_id, identifiers, category, state}
            SQL: each snapshot in in-memory SQLite, replicas kept separate | JSON / logs / text: parsers
            PDF: pdfplumber (vision fallback for image-only pages) | screenshots: vision LLM, strict JSON
1 intent    multilingual deletion / negation / keep-account / request-type rules (EN ES PT FR DE IT JA KO ZH);
            texts with conflicting or missing signals -> batched LLM fallback
2 identity  request email (current or historical, case-insensitive) or its SHA-256 -> user_id;
            rebuild every email, phone, hash and name held up to audit_at from user_audit_log.csv
3 scope     the case's resources: its own deletion batch in system_activity.csv, evidence rows matching
            its identifiers, retained records; shared-batch ties broken by status binding and timing
4 state     newest evidence at or before audit_at per resource; replica drift; active syncs and
            restorable backups. Activity results count only as claims.
5 policy    deletion_rules.csv -> DELETE / REDACT / ANONYMIZE / RETAIN, tombstone keeps only user_id,
            backups -> BLOCK_RESTORE, flows -> DISABLE_DATA_FLOW
6 steps     FINDING -> REMEDIATION -> VERIFICATION; operation-table order, then system_id, resource_id;
            downstream copies before their sources; evidence cited only if captured by audit_at
```

| Path | Contents |
|---|---|
| `code/main.py` | entry point, contract validator |
| `code/extract/` | evidence extractors, content-hash cache, provider-neutral LLM client with usage logging |
| `code/engine/` | intent, identity, scope, state, policy and pipeline stages |
| `code/prompts/` | `image_extraction.md`, `pdf_page_extraction.md`, `intent_classifier.md` |
| `code/docs/rules_derived.md` | rules derived from the 32 public samples |
| `code/evaluation/` | scorer, custom cases, `evaluation_report.md`, `usage_report.md`, `iterations.md` |

## Results

On the 32 public samples: status accuracy 100%, step P/R/F1 97.4/95.9/96.6%, sequence accuracy 71.9%.
On the 518 evaluation cases: 151 COMPLETE, 357 INCOMPLETE and 10 NOT_APPLICABLE. The final run made
0 model calls because everything was served from the cache, and the cache itself cost about 31k
free-tier tokens. Details are in `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`.

## Packaging

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

Submit `code.zip`, `output.csv` and `log.txt` (chat transcript) at
https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
