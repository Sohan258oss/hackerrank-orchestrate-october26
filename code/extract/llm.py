"""Vision LLM client for free-tier, OpenAI-compatible chat-completions endpoints.

VISION_PROVIDER picks a preset (endpoint, default model, and which key
variable to read). Each provider only ever reads its own key variable, so a
key is never sent to another provider's endpoint:

    gemini (default)  key GEMINI_API_KEY
                      https://generativelanguage.googleapis.com/v1beta/openai
                      model gemini-3.8-flash (41/41 on the screenshot field checks)
    nvidia (fallback) key VISION_API_KEY or NVIDIA_API_KEY
                      https://integrate.api.nvidia.com/v1
                      model nvidia/nemotron-3.5-super-vl-120b-a12b (39/41, faster)

Overrides:
    VISION_BASE_URL    any OpenAI-compatible base URL
    VISION_MODEL       model id
    VISION_PROVIDER    preset name, also written to the usage log
    VISION_JSON_MODE   "json_object" to send response_format, default off
    VISION_MAX_TOKENS  default 16384 (reasoning models spend most output on hidden reasoning)
    VISION_TIMEOUT     seconds per HTTP attempt, default 300
    VISION_MAX_B64_CHARS  inline image size cap before re-encoding, default 180000

Every HTTP attempt is appended to .cache/llm_usage.jsonl (no secrets).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image

from . import env

PROMPTS_DIR = env.REPO_ROOT / "code" / "prompts"
USAGE_LOG = env.CACHE_ROOT / "llm_usage.jsonl"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

PROVIDERS: dict[str, dict[str, Any]] = {
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "key_vars": ["VISION_API_KEY", "NVIDIA_API_KEY"],
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.8-flash",
        "key_vars": ["GEMINI_API_KEY"],
    },
}
DEFAULT_PROVIDER = "gemini"


class VisionUnavailable(RuntimeError):
    """Raised when no API key is configured; callers record the file as unprocessed."""


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def prompt_hash(name: str) -> str:
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()[:16]


def log_usage(entry: dict[str, Any]) -> None:
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": RUN_ID, **entry}
    with USAGE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_cache_hit(file_id: str, prompt_name: str, provider: str, model: str) -> None:
    log_usage({"provider": provider, "model": model, "file_id": file_id, "prompt": prompt_name,
               "attempt": 0, "status": "cache_hit", "cached": True, "input_tokens": 0, "output_tokens": 0})


def image_bytes_from_path(path: Path) -> tuple[bytes, str]:
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
        path.suffix.lower(), "image/png")
    return path.read_bytes(), mime


def encode_image(data: bytes, mime: str, max_chars: int) -> tuple[str, str]:
    """Base64-encode, re-encoding as JPEG and downscaling only if the payload is too large."""
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) <= max_chars:
        return encoded, mime
    image = Image.open(io.BytesIO(data)).convert("RGB")
    for scale in (1.0, 0.85, 0.7, 0.55):
        frame = image if scale == 1.0 else image.resize((int(image.width * scale), int(image.height * scale)))
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=90)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        if len(encoded) <= max_chars:
            break
    return encoded, "image/jpeg"


def parse_json_text(text: str) -> Any:
    """Parse model output that should be one JSON object, tolerating code fences or stray prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start:end + 1])


class VisionClient:
    def __init__(self) -> None:
        self.provider = (env.get("VISION_PROVIDER") or DEFAULT_PROVIDER).lower()
        preset = PROVIDERS.get(self.provider, {"base_url": None, "model": None,
                                               "key_vars": [f"{self.provider.upper()}_API_KEY"]})
        self.api_key = env.get_first(preset["key_vars"])
        self.base_url = (env.get("VISION_BASE_URL") or preset["base_url"] or "").rstrip("/")
        self.model = env.get("VISION_MODEL") or preset["model"] or ""
        if self.api_key and not (self.base_url and self.model):
            raise ValueError(f"provider {self.provider!r} needs VISION_BASE_URL and VISION_MODEL set")
        self.json_mode = (env.get("VISION_JSON_MODE") or "").lower() == "json_object"
        self.max_tokens = int(env.get("VISION_MAX_TOKENS") or 16384)
        self.max_b64 = int(env.get("VISION_MAX_B64_CHARS") or 180000)
        self.timeout = int(env.get("VISION_TIMEOUT") or 300)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def identity(self) -> str:
        """Cache-key component: results depend on provider endpoint and model."""
        return f"{self.provider}|{self.base_url}|{self.model}"

    def extract_json(self, image_bytes: bytes, mime: str, prompt_name: str, file_id: str,
                     validate: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        """Send one image with a strict-JSON prompt; validate, with one repair round-trip."""
        if not self.available:
            names = " or ".join(PROVIDERS.get(self.provider, {}).get("key_vars", [f"{self.provider.upper()}_API_KEY"]))
            raise VisionUnavailable(f"set {names} in the environment or .env")
        b64, sent_mime = encode_image(image_bytes, mime, self.max_b64)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": [
                {"type": "text", "text": load_prompt(prompt_name)},
                {"type": "image_url", "image_url": {"url": f"data:{sent_mime};base64,{b64}"}},
            ],
        }]
        return self._json_round_trip(messages, prompt_name, file_id, validate)

    def complete_json(self, prompt_name: str, user_text: str, file_id: str,
                      validate: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        """Text-only variant: prompt file followed by `user_text`, strict JSON back."""
        if not self.available:
            names = " or ".join(PROVIDERS.get(self.provider, {}).get("key_vars", [f"{self.provider.upper()}_API_KEY"]))
            raise VisionUnavailable(f"set {names} in the environment or .env")
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": f"{load_prompt(prompt_name)}\n\n{user_text}",
        }]
        return self._json_round_trip(messages, prompt_name, file_id, validate)

    def _json_round_trip(self, messages: list[dict[str, Any]], prompt_name: str, file_id: str,
                         validate: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        """Validate the reply; on failure, show the model its output and the error once."""
        text = self._post(messages, file_id, prompt_name, attempt=1)
        try:
            return validate(parse_json_text(text))
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"That output was rejected ({type(exc).__name__}: {exc}). Reply again with only the "
                    "single JSON object described in the instructions: no prose, no code fences.")},
            ]
            return validate(parse_json_text(self._post(messages, file_id, prompt_name, attempt=2)))

    def _post(self, messages: list[dict[str, Any]], file_id: str, prompt_name: str, attempt: int) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "stream": False,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        last_error = ""
        for retry in range(4):
            started = time.monotonic()
            try:
                resp = requests.post(f"{self.base_url}/chat/completions", json=body, headers=headers,
                                     timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = type(exc).__name__
                self._log(file_id, prompt_name, attempt, None, "connection_error", started)
                time.sleep(2 ** retry * 2)
                continue
            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}"
                self._log(file_id, prompt_name, attempt, None, f"http_{resp.status_code}", started)
                try:
                    wait = float(resp.headers.get("retry-after") or 2 ** retry * 3)
                except ValueError:
                    wait = 2 ** retry * 3
                time.sleep(min(wait, 60))
                continue
            if resp.status_code >= 400:
                self._log(file_id, prompt_name, attempt, None, f"http_{resp.status_code}", started)
                raise RuntimeError(f"vision call failed: HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            self._log(file_id, prompt_name, attempt, payload.get("usage") or {}, "ok", started)
            choices = payload.get("choices") or []
            if not choices:
                raise RuntimeError("vision call returned no choices")
            return choices[0].get("message", {}).get("content") or ""
        raise RuntimeError(f"vision call failed after retries: {last_error}")

    def _log(self, file_id: str, prompt_name: str, attempt: int, usage: dict[str, Any] | None,
             status: str, started: float) -> None:
        usage = usage or {}
        log_usage({
            "provider": self.provider,
            "model": self.model,
            "file_id": file_id,
            "prompt": prompt_name,
            "attempt": attempt,
            "status": status,
            "cached": False,
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "latency_s": round(time.monotonic() - started, 2),
        })
