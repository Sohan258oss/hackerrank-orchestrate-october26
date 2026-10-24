# Usage report

The pipeline is mostly deterministic. Models are used in exactly two places, and both are cached
by content hash:

| Use | Provider | Model | When it is called |
|---|---|---|---|
| Screenshot extraction (vision, strict JSON) | Google Gemini, OpenAI-compatible endpoint | `gemini-3.8-flash` | once per screenshot, or for a PDF page with no text layer |
| Request-intent fallback (text, batched by 20) | Google Gemini | `gemini-3.8-flash` | only for request texts the multilingual rules cannot decide |

Every call and every cache hit is logged with provider, model, prompt name, and input/output tokens
to `.cache/llm_usage.jsonl`. `code/main.py` prints a per-model summary for the run at the end.

## Final full-dataset run (the one that wrote `output.csv`)

`python code/main.py` over all 518 evaluation cases:

| Provider / model | Prompt | Live calls | Cache hits | Input tokens | Output tokens |
|---|---|---|---|---|---|
| gemini / gemini-3.8-flash | image_extraction | 0 | 11 | 0 | 0 |
| gemini / gemini-3.8-flash | intent_classifier | 0 | 0 | 0 | 0 |
| **Total** | | **0** | **11** | **0** | **0** |

- All 11 screenshots came from the extraction cache built in the run below; the other 83 evidence
  files are parsed locally (SQLite, JSON, log and text parsers, `pdfplumber`).
- All 518 request texts were decided by the rules, so the intent fallback made no calls.
- Total tokens: 0, average 0 per request. Cost: **$0.00** total and per request.

## Cold-start cost (building the cache the final run used)

These are the calls that produced the cached screenshot results. Any fresh clone with
`GEMINI_API_KEY` set repeats them once on its first run.

| Provider / model | Prompt | Calls | Input tokens | Output tokens | Total tokens |
|---|---|---|---|---|---|
| gemini / gemini-3.8-flash | image_extraction (11 screenshots) | 12 | ~24,600 | ~6,700 | ~31,300 |
| gemini / gemini-3.8-flash | intent_classifier | 0 | 0 | 0 | 0 |
| **Total** | | **12** | **~24,600** | **~6,700** | **~31,300** |

- Average per evaluation request: ~31,300 / 518 ≈ **60 tokens** (≈ 47 input + 13 output).
- Wall time: about 28 minutes (median ~187 s per screenshot on the free tier).
- Cost: **$0.00**. Every call ran on the Gemini API free tier, so no charge was incurred, and per-request
  cost is $0.00. At paid rates, cost scales linearly with the ~31k tokens above.

## Development-only calls (not part of the submission run)

Listed for transparency. These calls did not produce `output.csv`.

| Purpose | Provider / model | Calls | Input tokens | Output tokens |
|---|---|---|---|---|
| Vision model comparison, run 1 | nvidia / nemotron-3.5-super-vl-120b-a12b | 14 | ~28,600 | ~45,600 |
| Vision model comparison, run 2 | nvidia / nemotron-3.5-super-vl-120b-a12b | 13 | ~30,800 | ~51,700 |
| Intent fallback test on 5 synthetic texts | gemini / gemini-3.8-flash | 1 | 734 | 362 |
| Timed-out attempts (llama-3.2-90b-vision) | nvidia / meta/llama-3.2-90b-vision-instruct | 3 | 0 | 0 |

All of these ran on free tiers ($0.00). Nemotron was replaced by Gemini after scoring 39/41 against
Gemini's 41/41 on hand-checked screenshot fields. Nemotron also spent most of its output tokens on
reasoning, as the token counts show.
