from __future__ import annotations

import json
from typing import Any

import streamlit as st
from groq import Groq

import ai_gateway
from config import DEFAULT_GROQ_MODEL, GROQ_MODELS, get_secret


class GroqServiceError(RuntimeError):
    """Safe, user-facing Groq configuration/API error."""


def _clean(value: str | None) -> str:
    return str(value or "").strip()


@st.cache_resource(show_spinner=False)
def get_client(api_key: str) -> Groq | None:
    """Create one cached Groq client for the supplied API key."""
    key = _clean(api_key)
    if not key:
        return None
    return Groq(api_key=key)


def _api_key() -> str:
    return _clean(get_secret("GROQ_API_KEY"))


def current_model() -> str:
    selected = _clean(st.session_state.get("llm_model"))
    # Prevent an old Gemini/deprecated model value from reaching Groq.
    return selected if selected in GROQ_MODELS else DEFAULT_GROQ_MODEL


def _compact(text: str, max_chars: int = 14000) -> str:
    """Keep RAG prompts small enough for Groq free/developer token limits."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[Context truncated for reliability.]"


def fallback_chain(selected: str) -> list[str]:
    """The chosen model first, then every other configured model as a backup."""
    return [selected] + [m for m in GROQ_MODELS if m != selected]


def generate_text(
    prompt: str,
    model: str | None = None,
    json_output: bool = False,
    max_completion_tokens: int = 3500,
    cache_ttl: float = 0,
) -> str:
    """Ask Groq. Retries rate limits/timeouts, then falls back to the other model before giving up.

    ``cache_ttl`` (seconds) reuses an identical earlier answer; leave it 0 for anything that must be fresh (quizzes).
    """
    key = _api_key()
    if not key:
        raise GroqServiceError(
            "GROQ_API_KEY is missing. Add GROQ_API_KEY to Streamlit Cloud → Settings → Secrets."
        )

    selected_model = _clean(model) if model else current_model()
    if selected_model not in GROQ_MODELS:
        selected_model = DEFAULT_GROQ_MODEL

    client = get_client(key)
    if client is None:
        raise GroqServiceError("Groq client could not be initialized. Check GROQ_API_KEY.")

    compact = _compact(prompt)
    ck = ai_gateway.cache_key(selected_model, json_output, max_completion_tokens, compact)
    cached = ai_gateway.cache_get(ck, cache_ttl)
    if cached is not None:
        return cached

    def call(model_name: str) -> str:
        # GPT-OSS supports reasoning_effort. Low keeps educational requests fast and
        # avoids unnecessary token consumption on Groq's token limits.
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "user", "content": compact}],
            "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": "low",
        }
        if json_output:
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("empty response")      # treated like a temporary fault: retried / next model
        return text

    try:
        text, used = ai_gateway.run_with_resilience(call, fallback_chain(selected_model))
    except ai_gateway.AIUnavailable as exc:
        raise GroqServiceError(str(exc)) from exc
    if used != selected_model:
        st.session_state["ai_fallback_notice"] = f"Answered by backup model {used} (your selected model was busy)."
    if cache_ttl > 0:
        ai_gateway.cache_put(ck, text)
    return text


def generate_json(prompt: str, model: str | None = None) -> Any:
    raw = generate_text(prompt, model=model, json_output=True, max_completion_tokens=5000)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise GroqServiceError("Groq returned an invalid JSON response. Please try again.")


def grounded_answer(question: str, context: str, memories: str = "") -> str:
    prompt = f"""You are Prep AI, a source-grounded educational assistant.
Answer the student's request using ONLY the provided learning context when the request is about the supplied material.
If the answer is not available in the context, clearly say it is not available in the provided material instead of inventing it.
Use student memories only to personalize explanation style, not as factual evidence.

STUDENT REQUEST:
{question}

LONG-TERM MEMORY:
{_compact(memories, 3500)}

LEARNING CONTEXT:
{_compact(context, 12000)}
"""
    return generate_text(prompt, max_completion_tokens=2500, cache_ttl=600)


def test_groq_connection(model: str | None = None) -> tuple[bool, str]:
    """Small diagnostic request used by Settings."""
    try:
        answer = generate_text(
            "Reply with exactly: Prep AI Groq connection successful.",
            model=model,
            max_completion_tokens=100,
        )
        return True, answer
    except GroqServiceError as exc:
        return False, str(exc)
