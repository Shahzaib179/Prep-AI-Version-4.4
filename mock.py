"""Mock test mode: full-length, sectioned, timed practice exams that survive a page reload.

* Questions come from the question bank first (the student's own + tutor-shared), unseen ones first, so a mock needs no AI when the bank is big enough.
  Any shortfall can be filled with AI-generated questions, which are then saved to the student's bank for next time.
* Sections follow a blueprint. The MDCAT-style presets use the commonly published pattern (Biology 68, Chemistry 54, Physics 54, English 18,
  Logical Reasoning 6 = 200 MCQs in 210 minutes, no negative marking). Counts and time are editable because the official pattern can change.
* Progress (answers + start time) is stored in the database, so a refresh, a lost connection or a phone restart does not lose a 3-hour exam.
  The clock keeps running while the student is away.
Pure logic here (no Streamlit); screens live in mock_pages.py.
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime
from typing import Any, Callable

import bank
import progress
import question_tools as qt
from db_core import connect

NEGATIVE_PENALTY = 0.25
SECTION_ORDER = ["Biology", "Chemistry", "Physics", "English", "Logical Reasoning"]

BLUEPRINTS: dict[str, dict[str, Any]] = {
    "MDCAT-style full length (200 MCQs)": {"sections": [("Biology", 68), ("Chemistry", 54), ("Physics", 54), ("English", 18), ("Logical Reasoning", 6)], "minutes": 210, "negative": False},
    "MDCAT-style half length (100 MCQs)": {"sections": [("Biology", 34), ("Chemistry", 27), ("Physics", 27), ("English", 9), ("Logical Reasoning", 3)], "minutes": 105, "negative": False},
    "Quick mock (50 MCQs)": {"sections": [("Biology", 17), ("Chemistry", 14), ("Physics", 14), ("English", 4), ("Logical Reasoning", 1)], "minutes": 53, "negative": False},
}


def minutes_for(total_questions: int) -> int:
    """Same pace as the full paper: 210 minutes for 200 questions."""
    return max(1, round(total_questions * 1.05))


# ------------------------------------------------------------------ building a mock
def _clean(q: dict[str, Any], section: str) -> dict[str, Any]:
    keep = ("question", "options", "answer", "explanation", "concept", "difficulty", "topic")
    out = {k: q.get(k) for k in keep if q.get(k) is not None}
    out["subject"] = section
    out["section"] = section
    if q.get("id"):
        out["bank_id"] = q["id"]
    return out


def assemble(student_id: str, sections: list[tuple[str, int]], difficulty: str | None = None, seed: Any = None,
             generate: Callable[[str, int], list[dict[str, Any]]] | None = None, max_ai: int = 100,
             shuffle_opts: bool = True) -> dict[str, Any]:
    """Pick the questions for a mock. Returns {'questions': [...], 'report': {section: {...}}}.

    ``generate(subject, n)`` (optional) returns up to n fresh validated MCQs for a subject; at most ``max_ai`` are requested overall.
    """
    seed = seed if seed is not None else time.time_ns()
    rng = random.Random(str(seed))
    questions: list[dict[str, Any]] = []
    report: dict[str, dict[str, int]] = {}
    ai_budget = max(0, int(max_ai))
    used_hashes: set[str] = set()
    for subject, want in sections:
        if want <= 0:
            continue
        rows = bank.pick_for_student(student_id, subject, want, difficulty, seed=f"{seed}:{subject}")
        picked = []
        for r in rows:
            h = progress.qhash(r["question"])
            if h not in used_hashes:
                used_hashes.add(h)
                picked.append(_clean(r, subject))
        from_bank, from_ai = len(picked), 0
        short = want - len(picked)
        if short > 0 and generate and ai_budget > 0:
            n = min(short, ai_budget)
            try:
                fresh = generate(subject, n) or []
            except Exception:           # AI unavailable: keep what the bank gave us
                fresh = []
            added = []
            for q in fresh:
                q = qt.normalize_question(q)
                h = progress.qhash(q.get("question", ""))
                if qt.validate_question(q) or h in used_hashes:
                    continue
                used_hashes.add(h)
                added.append(q)
                if len(added) >= n:
                    break
            if added:
                bank.bulk_add(student_id, added, subject, "Mock fill", source="ai")   # reused by future mocks
            picked += [_clean(q, subject) for q in added]
            from_ai = len(added)
            ai_budget -= n
        rng.shuffle(picked)
        questions += picked
        report[subject] = {"wanted": want, "bank": from_bank, "ai": from_ai, "missing": max(0, want - len(picked))}
    if shuffle_opts:
        questions = [qt.shuffle_options(q, f"{seed}:{progress.qhash(q['question'])}") for q in questions]
    return {"questions": questions, "report": report}


def section_names(questions: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for q in questions:
        s = q.get("section") or q.get("subject") or "General"
        if s not in seen:
            seen.append(s)
    return sorted(seen, key=lambda s: SECTION_ORDER.index(s) if s in SECTION_ORDER else 99)


# ------------------------------------------------------------------ scoring
def section_scores(questions: list[dict[str, Any]], answers: dict[int, str], negative: bool = False) -> dict[str, Any]:
    per: dict[str, dict[str, Any]] = {}
    for i, q in enumerate(questions):
        s = q.get("section") or q.get("subject") or "General"
        d = per.setdefault(s, {"section": s, "total": 0, "correct": 0, "wrong": 0, "skipped": 0})
        d["total"] += 1
        chosen = answers.get(i)
        if not chosen:
            d["skipped"] += 1
        elif chosen == str(q.get("answer", "")).strip():
            d["correct"] += 1
        else:
            d["wrong"] += 1
    rows = []
    for s in section_names(questions):
        d = per[s]
        d["marks"] = round(d["correct"] - (NEGATIVE_PENALTY * d["wrong"] if negative else 0), 2)
        d["accuracy"] = round(100 * d["correct"] / max(1, d["correct"] + d["wrong"]), 1)     # of the questions attempted
        d["percent"] = round(100 * d["correct"] / max(1, d["total"]), 1)                       # of the whole section
        rows.append(d)
    total = sum(d["total"] for d in rows)
    correct = sum(d["correct"] for d in rows)
    wrong = sum(d["wrong"] for d in rows)
    marks = round(correct - (NEGATIVE_PENALTY * wrong if negative else 0), 2)
    return {"sections": rows, "total": total, "correct": correct, "wrong": wrong, "skipped": sum(d["skipped"] for d in rows),
            "marks": marks, "max_marks": float(total), "percent": round(100 * marks / total, 1) if total else 0.0, "negative": negative}


def focus_advice(scores: dict[str, Any]) -> list[str]:
    """Plain-language next steps from the section results."""
    rows = [r for r in scores["sections"] if r["total"] >= 3]
    tips = []
    if rows:
        weakest = min(rows, key=lambda r: r["percent"])
        strongest = max(rows, key=lambda r: r["percent"])
        if weakest["section"] != strongest["section"]:
            tips.append(f"Weakest section: **{weakest['section']}** ({weakest['percent']:.0f}%). Strongest: **{strongest['section']}** ({strongest['percent']:.0f}%).")
        for r in rows:
            if r["skipped"] >= max(3, 0.2 * r["total"]):
                tips.append(f"You skipped {r['skipped']} of {r['total']} in **{r['section']}**: practise pacing or guess strategically if there is no negative marking.")
            if scores["negative"] and r["wrong"] > r["correct"] and r["wrong"] >= 5:
                tips.append(f"In **{r['section']}** wrong answers outnumber correct ones, which costs marks under negative marking. Skip when truly unsure.")
    return tips


# ------------------------------------------------------------------ results history
def save_result(student_id: str, quiz_id: int | None, blueprint: str, scores: dict[str, Any], taken_sec: int | None) -> int:
    with connect() as con:
        return con.insert(
            "INSERT INTO mock_results(student_id,quiz_id,blueprint,total,correct,wrong,skipped,marks,max_marks,negative,taken_sec,sections_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (student_id, quiz_id, blueprint, scores["total"], scores["correct"], scores["wrong"], scores["skipped"], scores["marks"], scores["max_marks"],
             int(scores["negative"]), taken_sec, json.dumps(scores["sections"]), datetime.utcnow().isoformat()))


def history(student_id: str, limit: int = 30) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM mock_results WHERE student_id=? ORDER BY id DESC LIMIT ?", (student_id, limit)).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["sections"] = json.loads(d.pop("sections_json") or "[]")
        d["percent"] = round(100 * d["marks"] / d["max_marks"], 1) if d["max_marks"] else 0.0
        out.append(d)
    return out


# ------------------------------------------------------------------ keep an exam alive across reloads
_STATE_KEYS = ("uid", "questions", "subject", "topic", "difficulty", "time_limit_sec", "started_at", "answers", "answer_ts", "negative", "blueprint", "mock")


def save_progress(student_id: str, quiz: dict[str, Any]) -> None:
    state = {k: quiz.get(k) for k in _STATE_KEYS}
    state["answers"] = {str(i): a for i, a in (quiz.get("answers") or {}).items()}
    state["answer_ts"] = {str(i): t for i, t in (quiz.get("answer_ts") or {}).items()}
    with connect() as con:
        con.execute("INSERT INTO mock_progress(student_id,state_json,updated_at) VALUES(?,?,?) ON CONFLICT(student_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                    (student_id, json.dumps(state), datetime.utcnow().isoformat()))


def load_progress(student_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT state_json FROM mock_progress WHERE student_id=?", (student_id,)).fetchone()
    if not row:
        return None
    s = json.loads(row[0])
    s["answers"] = {int(i): a for i, a in (s.get("answers") or {}).items()}
    s["answer_ts"] = {int(i): t for i, t in (s.get("answer_ts") or {}).items()}
    s.update({"sources": [], "finished": False, "result": None})
    return s


def clear_progress(student_id: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM mock_progress WHERE student_id=?", (student_id,))
