"""Resilience layer for LLM calls: retry with backoff, model fallback, concurrency limit, short-lived cache.

Pure Python (no Streamlit / Groq imports) so it is fast to test. ``groq_service`` plugs the real API call in.

Rules
* rate limit (429) and temporary faults (5xx, timeout, dropped connection): retry with exponential backoff + jitter
  (honours a ``Retry-After`` header), then move on to the next model in the fallback chain.
* invalid key (401): stop at once - no retry or fallback can fix it.
* model not permitted (403) or model missing (404): skip straight to the next model.
* request too large (413): stop at once - a different model will not fix it.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

AUTH, PERMISSION, RATE_LIMIT, TOO_LARGE, TRANSIENT, OTHER = "auth", "permission", "rate_limit", "too_large", "transient", "other"

MAX_RETRIES = 3                 # attempts per model = 1 + MAX_RETRIES (rate limit/transient only)
BACKOFF_BASE_SEC = 1.0          # waits ~1s, 2s, 4s (+ jitter)
BACKOFF_CAP_SEC = 15.0
MAX_CONCURRENT_CALLS = 4        # calls in flight at once across ALL students on this server
CACHE_MAX_ITEMS = 200


class AIUnavailable(RuntimeError):
    """Every attempt failed. ``kind`` tells the UI what to say; ``message`` is safe to show."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def classify_error(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    if status == 401 or "invalid api key" in text or "authentication" in name or "authentication" in text:
        return AUTH
    if status in (403, 404) or "permission" in name or "not permitted" in text or "blocked" in text or "model_not_found" in text or "does not exist" in text:
        return PERMISSION
    if status == 413 or "too large" in text or "request_too_large" in text or ("context" in text and "limit" in text):
        return TOO_LARGE
    if status == 429 or "rate limit" in text or "rate_limit" in text or "too many requests" in text or "ratelimit" in name:
        return RATE_LIMIT
    if (isinstance(status, int) and status >= 500) or "timeout" in name or "timed out" in text or "connection" in name or "connection" in text or "overloaded" in text or "unavailable" in text:
        return TRANSIENT
    return OTHER


def retry_after_seconds(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        value = headers.get("retry-after") if headers else None
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass
class Stats:
    calls: int = 0
    retries: int = 0
    fallbacks: int = 0
    failures: int = 0
    cache_hits: int = 0
    last_error_kind: str = ""
    by_model: dict[str, int] = field(default_factory=dict)


STATS = Stats()
_LIMITER = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


def cache_key(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def cache_get(key: str, ttl: float, now: Callable[[], float] = time.time) -> Any | None:
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now() - hit[0] <= ttl:
            STATS.cache_hits += 1
            return hit[1]
        _CACHE.pop(key, None)
    return None


def cache_put(key: str, value: Any, now: Callable[[], float] = time.time) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_MAX_ITEMS:
            _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)   # drop the oldest entry
        _CACHE[key] = (now(), value)


def friendly_message(kind: str, models_tried: Sequence[str]) -> str:
    if kind == AUTH:
        return "The Groq API key is invalid or expired. Ask the app owner to update GROQ_API_KEY."
    if kind == RATE_LIMIT:
        return ("The AI service is very busy right now (rate limit reached, even after retrying and trying the backup model). "
                "Please wait about a minute and try again, or ask for fewer questions.")
    if kind == TOO_LARGE:
        return "The request is too large for the AI model. Try a more specific topic, fewer questions or a smaller document."
    if kind == PERMISSION:
        return f"None of the configured models ({', '.join(models_tried)}) is allowed for this Groq project. Check the project's model access."
    if kind == TRANSIENT:
        return "The AI service could not be reached (temporary network or server problem). Please try again in a moment."
    return "The AI request failed. Please try again; if it keeps failing, check the API key and model settings."


def run_with_resilience(
    call: Callable[[str], Any],
    models: Sequence[str],
    *,
    max_retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] | None = None,
    rng: Callable[[], float] | None = None,
) -> tuple[Any, str]:
    """Call ``call(model)`` following the rules in the module docstring. Returns ``(result, model_used)``."""
    sleep = sleep or time.sleep
    rng = rng or random.random
    if not models:
        raise AIUnavailable(OTHER, "No AI model is configured.")
    STATS.calls += 1
    last_kind = OTHER
    for m_index, model in enumerate(models):
        attempt = 0
        while True:
            try:
                with _LIMITER:
                    result = call(model)
                STATS.by_model[model] = STATS.by_model.get(model, 0) + 1
                if m_index > 0:
                    STATS.fallbacks += 1
                return result, model
            except AIUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                kind = classify_error(exc)
                last_kind = kind
                STATS.last_error_kind = kind
                if kind in (AUTH, TOO_LARGE):
                    STATS.failures += 1
                    raise AIUnavailable(kind, friendly_message(kind, models)) from exc
                if kind == PERMISSION:
                    break                                   # next model
                if kind in (RATE_LIMIT, TRANSIENT) and attempt < max_retries:
                    wait = retry_after_seconds(exc)
                    if wait is None:
                        wait = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** attempt)) * (0.75 + 0.5 * rng())
                    sleep(min(wait, BACKOFF_CAP_SEC))
                    attempt += 1
                    STATS.retries += 1
                    continue
                if kind == OTHER and attempt < 1:           # unknown error: one quick retry, then fall back
                    attempt += 1
                    STATS.retries += 1
                    sleep(0.5)
                    continue
                break                                       # retries exhausted -> next model
    STATS.failures += 1
    raise AIUnavailable(last_kind, friendly_message(last_kind, models))
