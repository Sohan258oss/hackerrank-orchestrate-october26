"""Content-addressed cache for extraction results.

Keys combine the evidence file's SHA-256 with everything that can change the
output (extractor version, and for LLM extractors the model and prompt hash),
so editing a prompt or switching models never serves a stale result.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .env import CACHE_ROOT

FILES_DIR = CACHE_ROOT / "extract" / "files"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def cache_key(file_hash: str, *parts: str) -> str:
    return sha256_bytes("|".join((file_hash,) + parts).encode("utf-8"))


def load(key: str) -> Any | None:
    path = FILES_DIR / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def store(key: str, value: Any) -> None:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILES_DIR / f"{key}.json.tmp"
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(FILES_DIR / f"{key}.json")
