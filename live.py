"""Live quiz (Kahoot style): a tutor hosts, students join with a code and answer the same question at the same time.

All timing is decided on the server: the host moves the game on, a student's answer is accepted only while the question is open,
and speed points are computed from the server's clock, never from anything the browser sends. Pure logic (no Streamlit).
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from typing import Any

import progress
import question_tools as qt
from db_core import connect, IntegrityError
from db_core import cached_read

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_POINTS = 1000
MIN_CORRECT_POINTS = 500       # a correct answer at the last second still earns half
GRACE_SEC = 1.5                # network delay allowed after the timer reaches zero
LOBBY, QUESTION, REVEAL, FINISHED = "lobby", "question", "reveal", "finished"


def now() -> float:
    return time.time()


def _norm(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


# ------------------------------------------------------------------ sessions
def create_session(host_id: str, title: str, questions: list[dict[str, Any]], seconds_per_q: int = 20) -> dict[str, Any]:
    clean = []
    for q in questions:
        q = qt.normalize_question(q)
        if not qt.validate_question(q):
            clean.append({k: q[k] for k in ("question", "options", "answer", "explanation", "concept", "difficulty")})
    if not clean:
        raise ValueError("Add at least one valid question.")
    seconds_per_q = max(5, min(120, int(seconds_per_q)))
    for _ in range(20):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        try:
            with connect() as con:
                sid = con.insert("INSERT INTO live_sessions(code,host_id,title,questions_json,state,q_index,seconds_per_q,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                 (code, host_id, (title or "Live quiz").strip()[:80], json.dumps(clean), LOBBY, -1, seconds_per_q, datetime.utcnow().isoformat()))
            return {"id": sid, "code": code, "total": len(clean)}
        except IntegrityError:
            continue
    raise RuntimeError("Could not generate a unique code.")


def get_questions(session_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        r = con.execute("SELECT questions_json FROM live_sessions WHERE id=?", (session_id,)).fetchone()
    return json.loads(r[0]) if r else []


def get_state(code: str) -> dict[str, Any] | None:
    """Light-weight row used for polling (no questions)."""
    with connect() as con:
        r = con.execute("SELECT id,code,host_id,title,state,q_index,q_started_at,seconds_per_q,created_at,finished_at FROM live_sessions WHERE code=?", (_norm(code),)).fetchone()
    return dict(r) if r else None


def total_questions(session_id: int) -> int:
    return len(get_questions(session_id))


def phase(row: dict[str, Any], t: float | None = None) -> str:
    """lobby / question / reveal / finished. A question turns into 'reveal' by itself when its time is up."""
    t = now() if t is None else t
    if row["state"] == QUESTION and row["q_started_at"] is not None and t >= row["q_started_at"] + row["seconds_per_q"]:
        return REVEAL
    return row["state"]


def seconds_left(row: dict[str, Any], t: float | None = None) -> float:
    t = now() if t is None else t
    if row["state"] != QUESTION or row["q_started_at"] is None:
        return 0.0
    return max(0.0, row["q_started_at"] + row["seconds_per_q"] - t)


def _owned(con: Any, code: str, host_id: str) -> dict[str, Any] | None:
    r = con.execute("SELECT id,state,q_index,seconds_per_q FROM live_sessions WHERE code=? AND host_id=?", (_norm(code), host_id)).fetchone()
    return dict(r) if r else None


def advance(code: str, host_id: str, expected_q_index: int | None = None, t: float | None = None) -> str:
    """Host presses Start / Next. Returns the new state. ``expected_q_index`` makes a double click harmless."""
    t = now() if t is None else t
    with connect() as con:
        s = _owned(con, code, host_id)
        if not s or s["state"] == FINISHED:
            return FINISHED if s else "denied"
        if expected_q_index is not None and s["q_index"] != expected_q_index:
            return s["state"]
        n = len(json.loads(con.execute("SELECT questions_json FROM live_sessions WHERE id=?", (s["id"],)).fetchone()[0]))
        nxt = s["q_index"] + 1
        if nxt >= n:
            con.execute("UPDATE live_sessions SET state=?, finished_at=? WHERE id=?", (FINISHED, datetime.utcnow().isoformat(), s["id"]))
            return FINISHED
        con.execute("UPDATE live_sessions SET state=?, q_index=?, q_started_at=? WHERE id=? AND q_index=?", (QUESTION, nxt, t, s["id"], s["q_index"]))
    return QUESTION


def reveal(code: str, host_id: str) -> bool:
    with connect() as con:
        s = _owned(con, code, host_id)
        if not s or s["state"] != QUESTION:
            return False
        con.execute("UPDATE live_sessions SET state=? WHERE id=?", (REVEAL, s["id"]))
    return True


def end_session(code: str, host_id: str) -> bool:
    with connect() as con:
        s = _owned(con, code, host_id)
        if not s:
            return False
        con.execute("UPDATE live_sessions SET state=?, finished_at=? WHERE id=? AND state<>?", (FINISHED, datetime.utcnow().isoformat(), s["id"], FINISHED))
    return True


@cached_read(30)
def host_sessions(host_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT s.id,s.code,s.title,s.state,s.created_at,(SELECT COUNT(*) FROM live_players p WHERE p.session_id=s.id) AS players FROM live_sessions s WHERE s.host_id=? ORDER BY s.id DESC LIMIT ?", (host_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ players
def join(code: str, student_id: str, name: str) -> tuple[bool, str, dict[str, Any] | None]:
    row = get_state(code)
    if not row:
        return False, "No live quiz found with that code.", None
    if row["state"] == FINISHED:
        return False, "This live quiz has already finished.", None
    if row["host_id"] == student_id:
        return False, "You are the host of this quiz.", None
    with connect() as con:
        con.execute("INSERT INTO live_players(session_id,student_id,name,score,correct,joined_at) VALUES(?,?,?,0,0,?) ON CONFLICT(session_id,student_id) DO NOTHING",
                    (row["id"], student_id, (name or student_id)[:60], datetime.utcnow().isoformat()))
    return True, "Joined.", row


def players(session_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT student_id,name,score,correct FROM live_players WHERE session_id=? ORDER BY score DESC, name", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def points_for(is_correct: bool, elapsed: float, limit: float) -> int:
    if not is_correct:
        return 0
    frac = 0.0 if limit <= 0 else min(1.0, max(0.0, elapsed / limit))
    return int(round(MAX_POINTS - (MAX_POINTS - MIN_CORRECT_POINTS) * frac))


def submit_answer(session_id: int, student_id: str, q_index: int, choice: str, t: float | None = None) -> dict[str, Any]:
    """Record a student's answer. choice is the ORIGINAL option letter. Returns {accepted, correct, points, reason}."""
    t = now() if t is None else t
    choice = (choice or "").strip().upper()
    with connect() as con:
        s = con.execute("SELECT state,q_index,q_started_at,seconds_per_q,questions_json FROM live_sessions WHERE id=?", (session_id,)).fetchone()
        if not s or s["state"] != QUESTION or s["q_index"] != q_index or s["q_started_at"] is None:
            return {"accepted": False, "reason": "This question is closed."}
        if t > s["q_started_at"] + s["seconds_per_q"] + GRACE_SEC:
            return {"accepted": False, "reason": "Time is up."}
        if not con.execute("SELECT 1 FROM live_players WHERE session_id=? AND student_id=?", (session_id, student_id)).fetchone():
            return {"accepted": False, "reason": "You have not joined this quiz."}
        q = json.loads(s["questions_json"])[q_index]
        if choice not in q["options"]:
            return {"accepted": False, "reason": "Invalid option."}
        correct = choice == q["answer"]
        elapsed = max(0.0, t - s["q_started_at"])
        pts = points_for(correct, elapsed, s["seconds_per_q"])
        cur = con.execute("INSERT INTO live_answers(session_id,student_id,q_index,choice,is_correct,points,ms_used,answered_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,student_id,q_index) DO NOTHING",
                          (session_id, student_id, q_index, choice, int(correct), pts, int(elapsed * 1000), datetime.utcnow().isoformat()))
        if cur.rowcount != 1:
            return {"accepted": False, "reason": "You already answered this question."}
        if correct:
            con.execute("UPDATE live_players SET score=score+?, correct=correct+1 WHERE session_id=? AND student_id=?", (pts, session_id, student_id))
    return {"accepted": True, "correct": correct, "points": pts}


def my_answer(session_id: int, student_id: str, q_index: int) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT choice,is_correct,points FROM live_answers WHERE session_id=? AND student_id=? AND q_index=?", (session_id, student_id, q_index)).fetchone()
    return dict(r) if r else None


def answer_counts(session_id: int, q_index: int) -> dict[str, int]:
    with connect() as con:
        rows = con.execute("SELECT choice, COUNT(*) FROM live_answers WHERE session_id=? AND q_index=? GROUP BY choice", (session_id, q_index)).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def answered_count(session_id: int, q_index: int) -> int:
    with connect() as con:
        return int(con.execute("SELECT COUNT(*) FROM live_answers WHERE session_id=? AND q_index=?", (session_id, q_index)).fetchone()[0])


def standings(session_id: int, limit: int = 10) -> list[dict[str, Any]]:
    return [{"rank": i, **p} for i, p in enumerate(players(session_id)[:limit], start=1)]


def my_rank(session_id: int, student_id: str) -> tuple[int, int] | None:
    ps = players(session_id)
    for i, p in enumerate(ps, start=1):
        if p["student_id"] == student_id:
            return i, len(ps)
    return None


def final_report(session_id: int) -> list[dict[str, Any]]:
    """Per-student totals for the host (and CSV export)."""
    n = total_questions(session_id)
    return [{**r, "total": n, "accuracy": round(100 * r["correct"] / n, 1) if n else 0.0} for r in standings(session_id, 100000)]


def finish_credit(session_id: int, student_id: str) -> None:
    """After the game, give each player normal credit (mastery, mistakes, seen-questions, XP) through the usual quiz path (called once per student)."""
    import db
    qs = get_questions(session_id)
    with connect() as con:
        rows = con.execute("SELECT q_index,choice FROM live_answers WHERE session_id=? AND student_id=?", (session_id, student_id)).fetchall()
        done = con.execute("SELECT 1 FROM quiz_attempts WHERE student_id=? AND shared_code=?", (student_id, f"LIVE{session_id}")).fetchone()
    if done or not qs:
        return
    try:
        db.record_quiz(student_id, "Live Quiz", f"Live {session_id}", qs, {int(r[0]): r[1] for r in rows}, "Medium", shared_code=f"LIVE{session_id}")
    except IntegrityError:
        pass   # already credited from another tab


def display_question(q: dict[str, Any], student_id: str, session_id: int) -> dict[str, Any]:
    """The question as THIS student sees it: options in their own order (so neighbours cannot copy letters)."""
    return qt.shuffle_options(q, f"live:{session_id}:{student_id}:{progress.qhash(q['question'])}")


def original_letter(q: dict[str, Any], shown: dict[str, Any], shown_letter: str) -> str:
    """Translate the letter the student pressed back to the letter in the stored question."""
    text = shown["options"].get(shown_letter, "")
    for k, v in q["options"].items():
        if v == text:
            return k
    return shown_letter
