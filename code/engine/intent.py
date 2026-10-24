"""Stage 1: does the request text authorize deletion?

Deterministic multilingual rules decide first. A text is sent to the LLM only
when the rules see conflicting signals (a deletion verb next to a negation or a
non-deletion request type) or no signal at all. LLM answers are batched,
cached by text hash (plus model and prompt), and every call is written to the
shared usage log by the client.

Rule signals:
  positive   deletion / erasure / account-closure verbs in EN, ES, PT, FR, DE,
             IT, JA, KO, ZH (scoped forms such as "remove me from ..." excluded)
  negation   a deletion verb under "do not / don't / not / never / without"
  keep       an explicit wish to keep or keep using the account or profile
  types      access, copy, export/portability, correction, marketing opt-out,
             login help, invoices
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from extract import cache as file_cache
from extract import llm
from extract.env import CACHE_ROOT

PROMPT = "intent_classifier"
BATCH_SIZE = 20
INTENT_CACHE = CACHE_ROOT / "engine" / "intent"

DELETION_VERB = r"(?:delet\w*|eras\w*|remov\w*|clos(?:e|ed|ing)|erasure)"

POSITIVE = [
    re.compile(r"\b(?:delet(?:e|ed|es|ing|ion)|eras(?:e|ed|ing|ure)|forget me|right to be forgotten)\b"),
    re.compile(r"\bremov(?:e|ed|es|ing|al)\b(?!\s+me\s+from)"),
    re.compile(r"\bclos(?:e|ed|ing)\b(?:\s+\w+){0,3}?\s+account\b|\baccount\b[^.]{0,20}\bclosed\b"),
    re.compile(r"\b(?:account|profile)\b[^.]{0,15}\b(?:is|was) gone\b"),
    # Spanish, Portuguese, French, German, Italian
    re.compile(r"\b(?:elimin(?:ar|e|en|ación|acion|ação|acao)|borr(?:ar|e|ado)|supresi[óo]n|suprimir)\b"),
    re.compile(r"\b(?:exclus[ãa]o|exclu(?:ir|a|am)|apag(?:ar|ue))\b"),
    re.compile(r"\b(?:suppression|supprim(?:er|ez|é|e)|effac(?:er|ez|ement))\b"),
    re.compile(r"\b(?:l[öo]schen|l[öo]schung)\b"),
    re.compile(r"\b(?:cancellazione|cancellare|eliminare)\b"),
    # Japanese, Korean, Chinese (no word boundaries)
    re.compile(r"削除|消去|抹消|삭제|탈퇴|删除|注销|刪除"),
]

NEGATION = [
    re.compile(rf"\b(?:do not|don't|dont|does not|doesn't|did not|didn't|not|never|without)\s+(?:\w+\s+){{0,3}}?{DELETION_VERB}"),
    re.compile(r"\bno\s+(?:quiero|deseo|solicito)\s+(?:\w+\s+){0,2}?(?:eliminar|borrar)\b"),
    re.compile(r"\bn[ãa]o\s+(?:quero\s+)?(?:excluir|apagar)\b"),
    re.compile(r"\bne\s+(?:pas\s+)?supprim\w*|\bne\s+supprim\w*\s+pas\b"),
    re.compile(r"削除しないで|削除しない|삭제하지\s*마|삭제하지\s*않"),
]

KEEP = [
    re.compile(r"\b(?:keep|leave)\s+(?:the|my|this)\s+(?:account|profile)\b"),
    re.compile(r"\bleave\s+(?:the|my)\s+[\w\s]{0,30}\b(?:active|in place|as it is)\b"),
    re.compile(r"\bcontinue\s+using\b"),
    re.compile(r"\bstill\s+(?:need|use|want)\b[^.]{0,40}\b(?:account|service|it)\b"),
]

REQUEST_TYPES = {
    "access": re.compile(r"\bwhat (?:personal )?(?:information|data) you hold\b|\binformation request\b|\baccess request\b"),
    "copy": re.compile(r"\b(?:send|give|provide|need|get)\s+(?:me\s+)?a copy\b|\bcopy of (?:the|my) (?:information|data)\b"),
    "export": re.compile(r"\bdownload my\b|\bportable\b|\bportability\b|\bexport (?:of )?my data\b"),
    "correction": re.compile(r"\b(?:misspelled|out of date|incorrect|correct it|correction|replace it|change the email|update my)\b"),
    "marketing_opt_out": re.compile(r"\bpromotional e-?mails?\b|\bmarketing (?:lists?|e-?mails?|messages)\b|\bunsubscribe\b|\bopt[- ]out\b"),
    "login_help": re.compile(r"\breset(?:ting)? (?:the |my )?(?:login|password)\b|\bcan't access\b|\bcannot access\b"),
    "invoices": re.compile(r"\binvoices?\b"),
}

REQUEST_TYPE_VALUES = {"deletion", "access", "copy", "export", "correction", "marketing_opt_out",
                       "login_help", "invoices", "keep_account", "other"}


@dataclass
class IntentResult:
    text_hash: str
    authorizes_deletion: bool | None
    source: str                      # rules | llm | llm_cache | unresolved
    confidence: str                  # high | medium | low
    request_type: str
    signals: dict[str, list[str]] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text.strip().lower())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def find_all(patterns: list[re.Pattern[str]], text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for p in patterns for m in p.finditer(text)]


def rule_classify(text: str) -> IntentResult:
    norm = normalize(text)
    negations = find_all(NEGATION, norm)
    # Positive matches inside a negated span ("don't delete") do not count as a request.
    masked = norm
    for start, end, _ in negations:
        masked = masked[:start] + " " * (end - start) + masked[end:]
    positives = find_all(POSITIVE, masked)
    keeps = find_all(KEEP, norm)
    types = sorted(name for name, pattern in REQUEST_TYPES.items() if pattern.search(norm))
    signals = {
        "positive": [p[2] for p in positives],
        "negation": [n[2] for n in negations],
        "keep": [k[2] for k in keeps],
        "types": types,
    }
    h = text_hash(text)
    against = bool(negations or keeps)

    if positives and not against and not types:
        return IntentResult(h, True, "rules", "high", "deletion", signals, "deletion request, no contrary signal")
    if not positives and (against or types):
        kind = types[0] if types else "keep_account"
        return IntentResult(h, False, "rules", "high", kind, signals,
                            "non-deletion request" + (" with explicit negation/keep" if against else ""))
    reason = "conflicting signals" if positives else "no deletion or non-deletion signal"
    return IntentResult(h, None, "unresolved", "low", "unknown", signals, reason)


# ---------------------------------------------------------------- LLM fallback

def _cache_key(h: str, client: llm.VisionClient) -> str:
    return file_cache.cache_key(h, PROMPT, client.identity(), llm.prompt_hash(PROMPT))


def _load_cached(key: str) -> dict[str, Any] | None:
    path = INTENT_CACHE / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _store_cached(key: str, value: dict[str, Any]) -> None:
    INTENT_CACHE.mkdir(parents=True, exist_ok=True)
    (INTENT_CACHE / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _validator(expected_ids: list[str]):
    def validate(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("expected an object with a 'results' array")
        out = {}
        for item in payload["results"]:
            if not isinstance(item, dict) or item.get("id") not in expected_ids:
                raise ValueError(f"unknown or missing id in {item!r}"[:200])
            if not isinstance(item.get("authorizes_deletion"), bool):
                raise ValueError(f"authorizes_deletion must be true/false for id {item.get('id')}")
            rtype = str(item.get("request_type") or "other")
            out[item["id"]] = {
                "authorizes_deletion": item["authorizes_deletion"],
                "request_type": rtype if rtype in REQUEST_TYPE_VALUES else "other",
                "confidence": str(item.get("confidence") or "medium"),
                "rationale": str(item.get("rationale") or "")[:300],
            }
        missing = [i for i in expected_ids if i not in out]
        if missing:
            raise ValueError(f"missing results for ids {missing}")
        return {"results": out}
    return validate


def llm_classify(pending: dict[str, str], client: llm.VisionClient) -> dict[str, IntentResult]:
    """Classify {text_hash: text} with the LLM in batches; cached per text."""
    results: dict[str, IntentResult] = {}
    uncached: dict[str, str] = {}
    for h, text in pending.items():
        cached = _load_cached(_cache_key(h, client))
        if cached is not None:
            llm.log_cache_hit(f"intent:{h[:12]}", PROMPT, client.provider, client.model)
            results[h] = IntentResult(h, cached["authorizes_deletion"], "llm_cache", cached["confidence"],
                                      cached["request_type"], {}, cached["rationale"])
        else:
            uncached[h] = text
    items = list(uncached.items())
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start:start + BATCH_SIZE]
        ids = [h[:12] for h, _ in batch]
        payload = json.dumps([{"id": i, "text": t} for i, (_, t) in zip(ids, batch)], ensure_ascii=False, indent=1)
        answer = client.complete_json(PROMPT, payload, f"intent_batch:{ids[0]}+{len(ids) - 1}", _validator(ids))
        for i, (h, _) in zip(ids, batch):
            item = answer["results"][i]
            _store_cached(_cache_key(h, client), item)
            results[h] = IntentResult(h, item["authorizes_deletion"], "llm", item["confidence"],
                                      item["request_type"], {}, item["rationale"])
    return results


def classify_all(texts: dict[str, str], use_llm: bool = True,
                 client: llm.VisionClient | None = None) -> dict[str, IntentResult]:
    """Classify {case_id: request_text}. Rules first; undecided texts go to the LLM once per distinct text."""
    by_case = {case_id: rule_classify(text) for case_id, text in texts.items()}
    pending = {r.text_hash: texts[c] for c, r in by_case.items() if r.authorizes_deletion is None}
    if pending and use_llm:
        client = client or llm.VisionClient()
        if client.available:
            answers = llm_classify(pending, client)
            for case_id, result in by_case.items():
                if result.authorizes_deletion is None and result.text_hash in answers:
                    answer = answers[result.text_hash]
                    answer.signals = result.signals
                    by_case[case_id] = answer
    return by_case
