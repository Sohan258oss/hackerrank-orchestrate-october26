# Ghost Data Still Exists: deletion-audit agent

HackerRank Orchestrate, October 2026. For every request in `dataset/deletion_requests.csv`, the
agent:

1. decides whether the person actually asked for deletion;
2. for each authorized deletion, reconstructs the person's current and historical identifiers and finds
   every copy of their data in the evidence;
3. applies `deletion_rules.csv` to each record as of the case's `audit_at`;
4. writes `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with ordered FINDING, REMEDIATION and
   VERIFICATION steps to `output.csv`.

The challenge contract is in `problem_statement.md`.

## Architecture: evidence extraction, then a 6-stage auditor

```text
                    dataset/evidence/ (94 files: SQL, JSON, logs, text, PDF, screenshots)
                                         |
 Stage 0  EXTRACT   one normalized record per resource sighting:
                    {file_id, captured_at, system_id, resource_id, identifiers, category, state, copy}
                    - SQL snapshots: loaded into in-memory SQLite, primary and replicas kept separate
                    - JSON exports, service logs, text records: deterministic parsers
                    - PDFs: pdfplumber text layer (vision fallback only for image-only pages)
                    - screenshots: vision LLM with a strict JSON schema, validated and repaired
                    - cached by file content hash -> .cache/extract/records.jsonl
                                         |
       deletion_requests.csv, user_identities.csv, user_audit_log.csv,
       system_activity.csv, deletion_rules.csv, evidence_files.csv
                                         |
 Stage 1  INTENT    does request_text authorize deletion?  multilingual rules for deletion verbs,
                    negation ("don't delete"), keep-account, and other request types (access, export,
                    correction, opt-out); undecided texts -> batched LLM fallback -> NOT_APPLICABLE or go
 Stage 2  IDENTITY  request email (current or historical, case-insensitive) or its SHA-256 -> user_id;
                    rebuild every email, phone, hash and name the person held up to audit_at
 Stage 3  SCOPE     which resources belong to this case: its deletion batch in system_activity.csv
                    (shared batches divided between cases), evidence matching the person's identifiers
                    in the request window, and records retained by policy
 Stage 4  STATE     each resource's state at audit_at from the newest evidence captured by then;
                    replica drift; active syncs and restorable backups. Workflow results are claims,
                    not proof.
 Stage 5  POLICY    deletion_rules.csv -> DELETE / REDACT / ANONYMIZE / RETAIN; account tombstone keeps
                    only the stable user_id; backups -> BLOCK_RESTORE; flows -> DISABLE_DATA_FLOW;
                    COMPLETE only if every record is compliant and nothing can recreate the data
 Stage 6  STEPS     FINDING -> REMEDIATION -> VERIFICATION, operation-table order, then system_id,
                    resource_id; flows disabled and restores blocked before records change; downstream
                    copies before their sources; evidence cited only if captured by audit_at
                                         |
                    output.csv (518 rows) + built-in contract validation
```

| Path | Contents |
|---|---|
| `code/main.py` | entry point: runs stage 0 and stages 1-6, writes and validates `output.csv` |
| `code/extract/` | evidence extractors, content-hash cache, provider-neutral LLM client with usage logging |
| `code/engine/` | `intent`, `identity`, `scope`, `state`, `policy` stages and the shared `pipeline` |
| `code/prompts/` | `image_extraction.md`, `pdf_page_extraction.md`, `intent_classifier.md` |
| `code/docs/rules_derived.md` | decision and ordering rules derived from the 32 public samples |
| `code/evaluation/` | scorer, custom cases, output validator, `evaluation_report.md`, `usage_report.md`, `iterations.md` |

## Deterministic first, LLMs only where needed

Most of the audit is lookup, joining, time comparison and policy application. Code does these
exactly and repeatably. A model would add cost, latency and run-to-run variation, and could invent
resource ids or evidence citations. So:

- **Everything structured is parsed by code:** SQL, JSON, logs, text records, PDF text, identities,
  activity, rules, state reconstruction, policy and step ordering. The same input always produces a
  byte-identical `output.csv`, and every step can be traced to a file, activity or audit row.
- **LLMs are used in only two places, where the input is not machine-readable:**

  | Where | Model | Why | Guard rails |
  |---|---|---|---|
  | Screenshot extraction (stage 0) | `gemini-3.8-flash` (vision) | 11 application screenshots contain identifiers only as pixels | strict JSON schema, validation and one repair retry; resource-id and state normalization; results cached by file hash |
  | Intent fallback (stage 1) | `gemini-3.8-flash` (text) | request texts whose rules give conflicting or no signals | batched by 20, constrained labels, cached by text hash; in the final run the rules decided all 518 texts, so no calls were made |

- **Every call is logged** with provider, model and input/output tokens to `.cache/llm_usage.jsonl`.
  Prompts live in `code/prompts/`. The vision provider can be switched to NVIDIA Nemotron
  (`VISION_PROVIDER=nvidia`).

The final run that produced `output.csv` made 0 new model calls, because results were served from the
cache. Building that cache took 12 `gemini-3.8-flash` calls (~31k tokens, free tier). See
`code/evaluation/usage_report.md`.

## Setup

Requires Python 3.10+ (tested on 3.13). Run from the repository root, with `dataset/` present there.

```bash
python3 -m venv .venv && source .venv/bin/activate      # optional (Windows: .venv\Scripts\activate)
pip install -r code/requirements.txt                    # pdfplumber, Pillow, requests
```

Create a `.env` file in the repository root. It is git-ignored; never commit it:

```dotenv
# Required for screenshot extraction. Free key: https://aistudio.google.com/apikey
GEMINI_API_KEY=your-key-here

# Optional: use NVIDIA Nemotron for vision instead
# VISION_PROVIDER=nvidia
# VISION_API_KEY=nvapi-...
```

Keys are read only from the environment or `.env`, never printed or logged. Without a key the
pipeline still runs and passes the contract checks, but the 11 screenshots are skipped and 8 evaluation
cases lose findings that only a screenshot proves.

## Run

```bash
python3 code/main.py
```

This processes all 518 cases and writes `./output.csv`. It prints the status distribution, the contract
check result and the model usage for the run. The first run with a key extracts the screenshots, which
takes about 25-30 minutes on the Gemini free tier. Later runs read the cache and finish in seconds.

| Flag | Effect |
|---|---|
| `--skip-extract` | reuse `.cache/extract/records.jsonl` from a previous run |
| `--no-llm` | intent rules only, no intent-fallback calls |
| `--set sample --out FILE` | run the 32 sample requests instead |
| `--debug FILE` | write per-case decisions and notes as JSONL |

## Evaluate

```bash
python3 code/evaluation/main.py              # predict the 32 public samples and score them
python3 code/evaluation/custom_cases.py      # 18 participant-made checks (offline)
python3 code/evaluation/validate_output.py   # independent contract check of ./output.csv
```

| Measure | Result |
|---|---|
| Case status accuracy (32 samples) | 100% (32/32) |
| Step precision / recall / F1 | 97.4% / 95.9% / 96.6% |
| Field accuracy (each field) | 95.9% (185/193) |
| Sequence accuracy | 71.9% (23/32) |
| Custom cases | 18/18 |
| `output.csv` contract violations | 0 (151 COMPLETE, 357 INCOMPLETE, 10 NOT_APPLICABLE) |

Full metrics, the vision-model comparison, failures and improvements are in
`code/evaluation/evaluation_report.md`. Tokens and cost are in `code/evaluation/usage_report.md`.

## Packaging and submission

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

Submit `code.zip`, `output.csv` and `log.txt` (chat transcript) at
https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
