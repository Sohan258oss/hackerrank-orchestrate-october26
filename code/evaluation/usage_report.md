# Usage report: final full-dataset run

This report covers only the run that produced the submitted `output.csv`: `python code/main.py` over
all 518 evaluation cases. Development and model-comparison calls are excluded; the vision comparison
is in `evaluation_report.md` §3.

## Models and OCR in the pipeline

| Stage | Provider | Model / engine | Use in the final run |
|---|---|---|---|
| Screenshot OCR + extraction (vision, strict JSON) | Google Gemini (OpenAI-compatible endpoint) | `gemini-3.8-flash` | 11 application screenshots |
| Image-only PDF pages (vision fallback) | Google Gemini | `gemini-3.8-flash` | not needed: every PDF has a text layer |
| PDF text extraction (OCR-free text layer) | local library | `pdfplumber` 0.11 | all PDFs; no tokens, no cost |
| Request-intent fallback (text) | Google Gemini | `gemini-3.8-flash` | not needed: rules decided all 518 requests |
| Vision fallback provider | NVIDIA NIM | `nvidia/nemotron-3.5-super-vl-120b-a12b` | not used; selectable via `VISION_PROVIDER=nvidia` |

SQL snapshots, JSON, logs and text records are parsed locally without any model.

## Source of the numbers

Every model call and cache hit is written to the usage log `.cache/llm_usage.jsonl` with provider,
model, prompt name, run id, and input and output tokens. `code/main.py` prints the per-model totals at
the end of each run. `.cache/` is git-ignored, so the figures below are copied from the run summaries
recorded in the chat transcript (`log.txt`). Token counts there were rounded to the nearest hundred and
are marked `~`.

The final run read all 11 screenshot results from the content-hash cache, so the run itself made 0 new
calls. Its model usage is the 12 Gemini calls that produced those cached results; a fresh clone with
a key repeats them once on its first run. Those calls are the totals below.

## Per-model and overall totals

| Provider | Model | Prompt | Calls | Input tokens | Output tokens | Total tokens |
|---|---|---|---|---|---|---|
| Google | `gemini-3.8-flash` | `image_extraction` (11 screenshots) | 12 | ~24,600 | ~6,700 | ~31,300 |
| Google | `gemini-3.8-flash` | `pdf_page_extraction` | 0 | 0 | 0 | 0 |
| Google | `gemini-3.8-flash` | `intent_classifier` | 0 | 0 | 0 | 0 |
| **Google** | **`gemini-3.8-flash` total** | | **12** | **~24,600** | **~6,700** | **~31,300** |
| NVIDIA | `nemotron-3.5-super-vl-120b-a12b` | fallback (unused) | 0 | 0 | 0 | 0 |
| local | `pdfplumber` | PDF text layer | n/a | 0 | 0 | 0 |
| **Overall** | | | **12** | **~24,600** | **~6,700** | **~31,300** |

## Per-request averages (÷ 518 requests)

| | Input | Output | Total |
|---|---|---|---|
| Tokens per request | ~47.5 | ~12.9 | **~60.4** |
| Model calls per request | | | 0.023 |

Almost all tokens go to the 11 screenshots (about 2,850 tokens each). The other cases need no model
call at all.

## Cost

Actual cost: **$0.00**. All calls ran on the Gemini API free tier.

Equivalent paid-tier cost for `gemini-3.8-flash`, standard tier. The rates come from third-party price
listings checked in October 2026 (for example pricepertoken.com and eesel.ai), so confirm them on
Google's pricing page.

| Price basis | Input $/1M | Output $/1M | Input cost | Output cost | **Total** | **Per request** |
|---|---|---|---|---|---|---|
| Free tier (used) | 0 | 0 | $0.00 | $0.00 | **$0.00** | **$0.00** |
| Paid, introductory rate (to 2026-12-31) | 0.75 | 3.75 | $0.0185 | $0.0251 | **$0.0436** | **$0.000084** |
| Paid, rate from 2027-01-01 | 1.50 | 7.50 | $0.0369 | $0.0503 | **$0.0872** | **$0.000168** |

Per-model cost: all of it is `gemini-3.8-flash`. Nemotron (unused) and `pdfplumber` (local) cost $0.

Time: the 12 vision calls took about 28 minutes on the free tier (median ~187 s per screenshot). With a
warm cache the full 518-case run takes about 1-2 seconds.
