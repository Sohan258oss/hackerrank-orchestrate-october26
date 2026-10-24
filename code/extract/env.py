"""Configuration lookup: process environment first, then the repo-root `.env` file.

Secrets are never read from anywhere else and never written to logs.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = REPO_ROOT / ".cache"


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def get(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    if value:
        return value
    return _dotenv().get(key) or default


def get_first(keys: list[str], default: str | None = None) -> str | None:
    for key in keys:
        value = get(key)
        if value:
            return value
    return default
