"""Question bank: reusable MCQs owned by a tutor (or a student, for AI questions saved from mock tests)."""
from __future__ import annotations

import json
import random
from datetime import datetime
from typing import Any

import progress
import question_tools as qt
from db_core import connect
from db_core import cached_read

SOURCES = ("manual", "ai", "quiz", "csv")


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["options"] = json.loads(d.pop("options_json") or "{}")
    d["question"] = d.pop("question_text")
    d["is_shared"] = bool(d.get("is_shared"))
    return d


_COLS = "id,owner_id,subject,topic,difficulty,question_text,options_json,answer,explanation,concept,source,qhash,is_shared,created_at,updated_at,times_used"


def add_question(owner_id: str, q: dict[str, Any], subject: str = "", topic: str = "", source: str = "manual", shared: bool = False) -> tuple[int | None, str]:
    """Insert one question. Returns (id, status) with status 'added', 'duplicate' or 'invalid: ...'."""
    q = qt.normalize_question(q)
    problems = qt.validate_question(q)
    if problems:
        return None, "invalid: " + " ".join(problems)
    h = progress.qhash(q["question"])
    now = datetime.utcnow().isoformat()
    with connect() as con:
        if con.execute("SELECT 1 FROM bank_questions WHERE owner_id=? AND qhash=?", (owner_id, h)).fetchone():
            return None, "duplicate"
        qid = con.insert(
            "INSERT INTO bank_questions(owner_id,subject,topic,difficulty,question_text,options_json,answer,explanation,concept,source,qhash,is_shared,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner_id, (subject or q.get("subject") or "").strip(), (topic or q.get("topic") or "").strip(), q["difficulty"], q["question"], json.dumps(q["options"]), q["answer"],
             q["explanation"], q["concept"], source if source in SOURCES else "manual", h, int(shared), now, now))
    return qid, "added"


def bulk_add(owner_id: str, questions: list[dict[str, Any]], subject: str = "", topic: str = "", source: str = "csv", shared: bool = False) -> dict[str, Any]:
    out = {"added": 0, "duplicates": 0, "invalid": []}
    for i, q in enumerate(questions, start=1):
        _, status = add_question(owner_id, q, q.get("subject") or subject, q.get("topic") or topic, source, shared)
        if status == "added":
            out["added"] += 1
        elif status == "duplicate":
            out["duplicates"] += 1
        else:
            out["invalid"].append(f"#{i}: {status}")
    return out


def get_question(owner_id: str, qid: int) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute(f"SELECT {_COLS} FROM bank_questions WHERE id=? AND owner_id=?", (qid, owner_id)).fetchone()
    return _row(r) if r else None


def update_question(owner_id: str, qid: int, q: dict[str, Any], subject: str | None = None, topic: str | None = None, shared: bool | None = None) -> tuple[bool, str]:
    """Edit a question you own. Returns (ok, message)."""
    q = qt.normalize_question(q)
    problems = qt.validate_question(q)
    if problems:
        return False, " ".join(problems)
    h = progress.qhash(q["question"])
    with connect() as con:
        cur = con.execute("SELECT subject,topic,is_shared FROM bank_questions WHERE id=? AND owner_id=?", (qid, owner_id)).fetchone()
        if not cur:
            return False, "Question not found."
        clash = con.execute("SELECT 1 FROM bank_questions WHERE owner_id=? AND qhash=? AND id<>?", (owner_id, h, qid)).fetchone()
        if clash:
            return False, "Another question in your bank already has this exact text."
        con.execute("UPDATE bank_questions SET subject=?,topic=?,difficulty=?,question_text=?,options_json=?,answer=?,explanation=?,concept=?,qhash=?,is_shared=?,updated_at=? WHERE id=? AND owner_id=?",
                    (subject if subject is not None else cur["subject"], topic if topic is not None else cur["topic"], q["difficulty"], q["question"], json.dumps(q["options"]), q["answer"],
                     q["explanation"], q["concept"], h, int(shared if shared is not None else cur["is_shared"]), datetime.utcnow().isoformat(), qid, owner_id))
    return True, "Saved."


def delete_questions(owner_id: str, ids: list[int]) -> int:
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with connect() as con:
        return con.execute(f"DELETE FROM bank_questions WHERE owner_id=? AND id IN ({marks})", (owner_id, *ids)).rowcount


def _filters(owner_id: str, subject: str | None, topic: str | None, difficulty: str | None, search: str | None) -> tuple[str, list[Any]]:
    where, params = ["owner_id=?"], [owner_id]
    if subject:
        where.append("subject=?"); params.append(subject)
    if topic:
        where.append("LOWER(topic) LIKE ?"); params.append(f"%{topic.lower()}%")
    if difficulty:
        where.append("difficulty=?"); params.append(difficulty)
    if search:
        where.append("LOWER(question_text) LIKE ?"); params.append(f"%{search.lower()}%")
    return " AND ".join(where), params


@cached_read(30)
def list_questions(owner_id: str, subject: str | None = None, topic: str | None = None, difficulty: str | None = None, search: str | None = None,
                   limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
    where, params = _filters(owner_id, subject, topic, difficulty, search)
    with connect() as con:
        rows = con.execute(f"SELECT {_COLS} FROM bank_questions WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, int(limit), int(offset))).fetchall()
    return [_row(r) for r in rows]


@cached_read(30)
def count_questions(owner_id: str, subject: str | None = None, topic: str | None = None, difficulty: str | None = None, search: str | None = None) -> int:
    where, params = _filters(owner_id, subject, topic, difficulty, search)
    with connect() as con:
        return int(con.execute(f"SELECT COUNT(*) FROM bank_questions WHERE {where}", params).fetchone()[0])


@cached_read(30)
def subject_counts(owner_id: str) -> dict[str, int]:
    with connect() as con:
        return {(r[0] or "Unsorted"): int(r[1]) for r in con.execute("SELECT subject, COUNT(*) FROM bank_questions WHERE owner_id=? GROUP BY subject ORDER BY 2 DESC", (owner_id,)).fetchall()}


def export_rows(owner_id: str, subject: str | None = None) -> list[dict[str, Any]]:
    return list_questions(owner_id, subject=subject, limit=100000)


def pick_questions(owner_id: str, count: int, subject: str | None = None, topic: str | None = None, difficulty: str | None = None,
                   seed: Any = None) -> list[dict[str, Any]]:
    """Random questions from one owner's bank for building a quiz (never more than exist)."""
    pool = list_questions(owner_id, subject=subject, topic=topic, difficulty=difficulty, limit=100000)
    random.Random(seed).shuffle(pool)
    return pool[:count]


def pick_for_student(student_id: str, subject: str, count: int, difficulty: str | None = None, seed: Any = None) -> list[dict[str, Any]]:
    """Questions a student may use in a mock test: their own bank plus anything tutors flagged as shared.

    Questions the student has not seen are used first.
    """
    sql = (f"SELECT {_COLS} FROM bank_questions WHERE subject=? AND (owner_id=? OR is_shared=1)" + (" AND difficulty=?" if difficulty else "") + " ORDER BY id DESC LIMIT 5000")
    params: list[Any] = [subject, student_id] + ([difficulty] if difficulty else [])
    with connect() as con:
        rows = [_row(r) for r in con.execute(sql, params).fetchall()]
    seen = progress.seen_hashes(student_id)
    rng = random.Random(seed)
    fresh = [r for r in rows if r["qhash"] not in seen]
    stale = [r for r in rows if r["qhash"] in seen]
    rng.shuffle(fresh); rng.shuffle(stale)
    picked, taken = [], set()
    for r in fresh + stale:
        if r["qhash"] in taken:
            continue
        taken.add(r["qhash"]); picked.append(r)
        if len(picked) >= count:
            break
    return picked


@cached_read(30)
def available_for_student(student_id: str, subject: str) -> int:
    with connect() as con:
        return int(con.execute("SELECT COUNT(DISTINCT qhash) FROM bank_questions WHERE subject=? AND (owner_id=? OR is_shared=1)", (subject, student_id)).fetchone()[0])


def mark_used(ids: list[int]) -> None:
    if ids:
        marks = ",".join("?" * len(ids))
        with connect() as con:
            con.execute(f"UPDATE bank_questions SET times_used=COALESCE(times_used,0)+1 WHERE id IN ({marks})", ids)
