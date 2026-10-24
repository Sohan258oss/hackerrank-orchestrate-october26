"""Normalize every file listed in dataset/evidence_files.csv into evidence records.

Usage (from the repository root):

    python code/extract/run.py                 # all files
    python code/extract/run.py --only evidence_01 evidence_94
    python code/extract/run.py --skip-vision   # deterministic formats only

Outputs (under .cache/extract/ by default):
    records.jsonl    one normalized record per line
    coverage.jsonl   per-table / per-snapshot coverage, for absence proofs
    status.json      per-file outcome (ok, cached, skipped_no_key, skipped_vision, error)

Each file's result is cached by content hash plus a hash of the extractor
code (and, for vision calls, model + prompt), so re-runs are free and any
code or prompt change invalidates exactly what it affects.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extract import (cache, image_extractor, json_extractor, llm, log_extractor,  # noqa: E402
                     pdf_extractor, sql_extractor, text_extractor)
from extract.common import EXTRACTOR_VERSION  # noqa: E402
from extract.env import CACHE_ROOT, REPO_ROOT  # noqa: E402

Extractor = Callable[[str, str, Path], tuple[list[dict[str, Any]], list[dict[str, Any]]]]

DETERMINISTIC: dict[str, Extractor] = {
    ".sql": sql_extractor.extract,
    ".json": json_extractor.extract,
    ".log": log_extractor.extract,
    ".txt": text_extractor.extract,
}
VISION_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def code_fingerprint() -> str:
    """Hash of the extractor sources: editing parsing logic invalidates cached results."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def rebind(items: list[dict[str, Any]], file_id: str, captured_at: str) -> list[dict[str, Any]]:
    for item in items:
        item["file_id"], item["captured_at"] = file_id, captured_at
    return items


def process(row: dict[str, str], dataset: Path, fingerprint: str, client: llm.VisionClient,
            skip_vision: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    file_id, captured_at = row["file_id"], row["captured_at"]
    path = dataset / row["file_path"]
    suffix = path.suffix.lower()
    status: dict[str, Any] = {"file_id": file_id, "file_path": row["file_path"], "captured_at": captured_at}
    if not path.is_file():
        return [], [], {**status, "status": "error", "error": "file not found"}
    if suffix in VISION_SUFFIXES and skip_vision:
        return [], [], {**status, "status": "skipped_vision"}

    file_hash = cache.sha256_file(path)
    status["sha256"] = file_hash
    parts = [suffix, EXTRACTOR_VERSION, fingerprint]
    if suffix in VISION_SUFFIXES or suffix == ".pdf":
        parts += [client.identity(), llm.prompt_hash(image_extractor.PROMPT), llm.prompt_hash(pdf_extractor.PROMPT)]
    key = cache.cache_key(file_hash, *parts)

    cached = cache.load(key)
    if cached is not None:
        return (rebind(cached["records"], file_id, captured_at), rebind(cached["coverage"], file_id, captured_at),
                {**status, "status": "cached", "records": len(cached["records"])})

    try:
        if suffix in DETERMINISTIC:
            records, coverage = DETERMINISTIC[suffix](file_id, captured_at, path)
        elif suffix == ".pdf":
            records, coverage = pdf_extractor.extract(file_id, captured_at, path, client)
        elif suffix in VISION_SUFFIXES:
            records, coverage = image_extractor.extract(file_id, captured_at, path, client)
        else:
            return [], [], {**status, "status": "error", "error": f"unsupported type {suffix}"}
    except llm.VisionUnavailable as exc:
        return [], [], {**status, "status": "skipped_no_key", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        return [], [], {**status, "status": "error", "error": f"{type(exc).__name__}: {exc}"}

    cache.store(key, {"records": records, "coverage": coverage})
    return records, coverage, {**status, "status": "ok", "records": len(records)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=REPO_ROOT / "dataset")
    parser.add_argument("--out", type=Path, default=CACHE_ROOT / "extract")
    parser.add_argument("--only", nargs="*", help="limit to these file_ids")
    parser.add_argument("--skip-vision", action="store_true", help="never call the vision model")
    args = parser.parse_args(argv)

    with (args.dataset / "evidence_files.csv").open(encoding="utf-8", newline="") as fh:
        manifest = list(csv.DictReader(fh))
    if args.only:
        wanted = set(args.only)
        manifest = [r for r in manifest if r["file_id"] in wanted]

    client = llm.VisionClient()
    if args.skip_vision:
        client.api_key = None  # PDFs still parse via pdfplumber; image-only pages report skipped_no_key
    fingerprint = code_fingerprint()
    all_records: list[dict[str, Any]] = []
    all_coverage: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    started = time.monotonic()
    for row in manifest:
        records, coverage, status = process(row, args.dataset, fingerprint, client, args.skip_vision)
        all_records += records
        all_coverage += coverage
        statuses.append(status)
        note = f"  {status['error']}" if status.get("error") else ""
        print(f"{status['file_id']:<12} {status['status']:<15} {status.get('records', 0):>4} records  "
              f"{row['file_path']}{note}")

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "records.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for record in all_records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with (args.out / "coverage.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for entry in all_coverage:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    by_status: dict[str, int] = {}
    for s in statuses:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    summary = {
        "run_id": llm.RUN_ID,
        "vision_provider": client.provider,
        "vision_model": client.model,
        "vision_key_configured": client.available,
        "files": len(statuses),
        "records": len(all_records),
        "by_status": by_status,
        "seconds": round(time.monotonic() - started, 1),
        "files_detail": statuses,
    }
    (args.out / "status.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{summary['files']} files -> {summary['records']} records; {by_status}; "
          f"vision={client.provider}/{client.model} key={'yes' if client.available else 'no'}")
    return 1 if by_status.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
