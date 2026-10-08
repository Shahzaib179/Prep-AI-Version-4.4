"""Pure helpers for MCQs: validation, normalisation, option shuffling, CSV import/export. No database, no Streamlit."""
from __future__ import annotations

import csv
import io
import random
import re
from typing import Any

LETTERS = ["A", "B", "C", "D"]
DIFFICULTIES = ["Easy", "Medium", "Hard"]
CSV_COLUMNS = ["subject", "topic", "difficulty", "question", "A", "B", "C", "D", "answer", "explanation", "concept"]

# Options that point at other options. Moving them would make the answer wrong, so such questions are never shuffled.
_POINTER = re.compile(
    r"(all|none|neither|any|both)\s+of\s+(the\s+)?(above|these|them)"
    r"|\bboth\s+\(?[a-d]\)?\s*(and|&|,)\s*\(?[a-d]\)?"
    r"|\b(option|choice)s?\s*\(?[a-d]\)?"
    r"|\babove\s+(are|is)\b|\ball\s+(the\s+)?above\b|\bnone\s+(of\s+)?the\s+above\b",
    re.I,
)
_SHORT_PAIR = re.compile(r"^\(?[a-d]\)?\s*(and|&|,|or)\s*\(?[a-d]\)?$", re.I)


def normalize_question(q: dict[str, Any]) -> dict[str, Any]:
    """Trim text, upper-case the answer letter, keep only A-D options. Does not invent content."""
    options = q.get("options") or {}
    out = {
        "question": " ".join(str(q.get("question", "")).split()),
        "options": {k: " ".join(str(options.get(k, "")).split()) for k in LETTERS if k in options},
        "answer": str(q.get("answer", "")).strip().upper()[:1],
        "explanation": str(q.get("explanation", "") or "").strip(),
        "concept": " ".join(str(q.get("concept", "") or "").split()),
        "difficulty": q.get("difficulty") if q.get("difficulty") in DIFFICULTIES else "Medium",
    }
    return out


def validate_question(q: dict[str, Any]) -> list[str]:
    """Human-readable problems; an empty list means the question is usable."""
    q = normalize_question(q)
    errors = []
    if len(q["question"]) < 10:
        errors.append("The question text is too short (at least 10 characters).")
    options = q["options"]
    if set(options) != set(LETTERS):
        errors.append("Exactly four options A, B, C and D are required.")
    else:
        if any(not v for v in options.values()):
            errors.append("Every option needs text.")
        texts = [v.lower() for v in options.values() if v]
        if len(set(texts)) != len(texts):
            errors.append("Two options have the same text.")
    if q["answer"] not in LETTERS:
        errors.append("The correct answer must be A, B, C or D.")
    elif q["answer"] in options and not options[q["answer"]]:
        errors.append("The correct answer points to an empty option.")
    return errors


def shuffle_blocked(q: dict[str, Any]) -> bool:
    for text in (q.get("options") or {}).values():
        t = str(text)
        if _POINTER.search(t) or (len(t) <= 25 and _SHORT_PAIR.match(t.strip())):
            return True
    return False


def shuffle_options(q: dict[str, Any], seed: Any) -> dict[str, Any]:
    """Return a copy with options re-ordered (re-lettered A-D) and the answer letter remapped.

    The order depends only on ``seed`` so the same student always sees the same order. Questions whose options
    refer to each other ("All of the above") are returned unchanged.
    """
    out = dict(q)
    options = q.get("options") or {}
    answer = str(q.get("answer", "")).strip().upper()
    if set(options) != set(LETTERS) or answer not in options or shuffle_blocked(q):
        return out
    keys = list(LETTERS)
    random.Random(str(seed)).shuffle(keys)
    out["options"] = {LETTERS[i]: options[old] for i, old in enumerate(keys)}
    out["answer"] = LETTERS[keys.index(answer)]
    return out


# ------------------------------------------------------------------ CSV
def to_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        o = r.get("options") or {}
        w.writerow([r.get("subject", ""), r.get("topic", ""), r.get("difficulty", "Medium"), r.get("question", ""), *(o.get(k, "") for k in LETTERS),
                    r.get("answer", ""), r.get("explanation", ""), r.get("concept", "")])
    return buf.getvalue()


_ALIASES = {"question_text": "question", "q": "question", "correct": "answer", "correct_answer": "answer", "option_a": "A", "option_b": "B", "option_c": "C", "option_d": "D",
            "a": "A", "b": "B", "c": "C", "d": "D", "level": "difficulty"}


def parse_csv(text: str, default_subject: str = "", default_topic: str = "", max_rows: int = 2000) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse an uploaded question file. Returns (good questions, error messages with row numbers)."""
    text = (text or "").lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["The file is empty."]
    cols = {}
    for name in reader.fieldnames:
        key = (name or "").strip()
        low = key.lower()
        cols[name] = _ALIASES.get(low, key if key in LETTERS else low)
    if "question" not in cols.values() or not {"A", "B", "C", "D"} <= set(cols.values()) or "answer" not in cols.values():
        return [], ["The first row must name the columns: question, A, B, C, D, answer (optional: subject, topic, difficulty, explanation, concept)."]
    good, errors = [], []
    for n, raw in enumerate(reader, start=2):
        if n - 1 > max_rows:
            errors.append(f"Stopped after {max_rows} rows.")
            break
        row = {cols[k]: (v or "").strip() for k, v in raw.items() if k is not None}
        if not any(row.values()):
            continue
        q = normalize_question({"question": row.get("question", ""), "options": {k: row.get(k, "") for k in LETTERS}, "answer": row.get("answer", ""),
                                "explanation": row.get("explanation", ""), "concept": row.get("concept", ""), "difficulty": row.get("difficulty", "Medium").title()})
        problems = validate_question(q)
        if problems:
            errors.append(f"Row {n}: " + " ".join(problems))
            continue
        q["subject"] = row.get("subject") or default_subject
        q["topic"] = row.get("topic") or default_topic
        good.append(q)
    return good, errors
