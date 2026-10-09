"""Daily challenge: the same 5 questions for every student each day, one attempt, bonus XP, a daily leaderboard and a streak calendar.

Question sources, in order: tutor-shared bank questions, AI (optional callback), the built-in starter set in evals/golden_questions.json
(so the challenge always works, even offline or when the AI is rate-limited).
Pure logic over the database (no Streamlit).
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import progress
import question_tools as qt
from db_core import connect, IntegrityError  # noqa: F401
from db_core import cached_read

QUESTIONS_PER_DAY = 5
BONUS_XP = 15            # for finishing the challenge
BONUS_PERFECT = 15       # extra for 5/5
MIX = [("Biology", 2), ("Chemistry", 1), ("Physics", 1), ("English", 1)]
GOLDEN = Path(__file__).resolve().parent / "evals" / "golden_questions.json"


def today() -> date:
    return progress.local_today()


def _golden() -> list[dict[str, Any]]:
    try:
        return json.loads(GOLDEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _shared_bank(subject: str) -> list[dict[str, Any]]:
    import bank
    with connect() as con:
        rows = con.execute(f"SELECT {bank._COLS} FROM bank_questions WHERE subject=? AND is_shared=1 ORDER BY id", (subject,)).fetchall()
    return [bank._row(r) for r in rows]


def _clean(q: dict[str, Any], subject: str) -> dict[str, Any] | None:
    q = qt.normalize_question(q)
    if qt.validate_question(q):
        return None
    return {k: q[k] for k in ("question", "options", "answer", "explanation", "concept", "difficulty")} | {"subject": subject}


def build_questions(day: date, generate: Callable[[str, int], list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """Pick the day's questions. Deterministic for a given day and data."""
    rng = random.Random(f"daily:{day.isoformat()}")
    chosen: list[dict[str, Any]] = []
    used: set[str] = set()
    golden = _golden()

    def take(pool: list[dict[str, Any]], subject: str, n: int) -> None:
        pool = list(pool)
        rng.shuffle(pool)
        for q in pool:
            if n <= 0:
                return
            c = _clean(q, subject)
            h = progress.qhash(c["question"]) if c else ""
            if c and h not in used:
                used.add(h)
                chosen.append(c)
                n -= 1

    for subject, n in MIX:
        before = len(chosen)
        take(_shared_bank(subject), subject, n)
        need = n - (len(chosen) - before)
        if need > 0 and generate:
            try:
                take(generate(subject, need) or [], subject, need)
            except Exception:  # noqa: BLE001 - AI trouble must never block the challenge
                pass
            need = n - (len(chosen) - before)
        if need > 0:
            take([g for g in golden if g.get("subject") == subject], subject, need)
    # still short (tiny golden pool): top up from anything
    if len(chosen) < QUESTIONS_PER_DAY:
        take(golden, "Mixed", QUESTIONS_PER_DAY - len(chosen))
    return chosen[:QUESTIONS_PER_DAY]


def get_challenge(day: date, generate: Callable[[str, int], list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """The day's shared question set. The first student to ask creates it; everyone else reads the same rows."""
    key = day.isoformat()
    with connect() as con:
        row = con.execute("SELECT questions_json FROM daily_challenges WHERE day=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    questions = build_questions(day, generate)
    if not questions:
        return []
    with connect() as con:
        con.execute("INSERT INTO daily_challenges(day,questions_json,subject_mix,created_at) VALUES(?,?,?,?) ON CONFLICT(day) DO NOTHING",
                    (key, json.dumps(questions), ",".join(f"{s}:{n}" for s, n in MIX), datetime.utcnow().isoformat()))
        row = con.execute("SELECT questions_json FROM daily_challenges WHERE day=?", (key,)).fetchone()   # another student may have won the race
    return json.loads(row[0])


def for_student(questions: list[dict[str, Any]], student_id: str, day: date) -> list[dict[str, Any]]:
    """Same questions for everyone, but each student sees them in their own order with their own option order."""
    qs = list(questions)
    random.Random(f"{day}:{student_id}").shuffle(qs)
    return [qt.shuffle_options(q, f"{day}:{student_id}:{progress.qhash(q['question'])}") for q in qs]


@cached_read(30)
def result_for(student_id: str, day: date) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT correct,total,taken_sec,xp,created_at FROM daily_results WHERE student_id=? AND day=?", (student_id, day.isoformat())).fetchone()
    return dict(r) if r else None


def record_result(student_id: str, day: date, correct: int, total: int, taken_sec: int | None) -> int:
    """Store the student's one result for the day and add the bonus XP. Returns the bonus (0 if already recorded)."""
    bonus = BONUS_XP + (BONUS_PERFECT if total and correct == total else 0)
    with connect() as con:
        cur = con.execute("INSERT INTO daily_results(student_id,day,correct,total,taken_sec,xp,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(student_id,day) DO NOTHING",
                          (student_id, day.isoformat(), int(correct), int(total), taken_sec, bonus, datetime.utcnow().isoformat()))
        stored = cur.rowcount == 1
    if not stored:
        return 0
    progress.add_bonus_xp(student_id, bonus, day)
    return bonus


@cached_read(30)
def leaderboard(day: date, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT r.student_id, COALESCE(s.name, r.student_id) AS name, r.correct, r.total, r.taken_sec FROM daily_results r "
                           "LEFT JOIN students s ON s.id=r.student_id WHERE r.day=? ORDER BY r.correct DESC, COALESCE(r.taken_sec, 999999) ASC, r.created_at ASC LIMIT ?",
                           (day.isoformat(), int(limit))).fetchall()
    return [{"rank": i, **dict(r)} for i, r in enumerate(rows, start=1)]


@cached_read(30)
def rank_of(student_id: str, day: date) -> tuple[int, int] | None:
    """(my rank, number of participants) for the day."""
    mine = result_for(student_id, day)
    if not mine:
        return None
    with connect() as con:
        better = con.execute("SELECT COUNT(*) FROM daily_results WHERE day=? AND (correct>? OR (correct=? AND COALESCE(taken_sec,999999)<?))",
                             (day.isoformat(), mine["correct"], mine["correct"], mine["taken_sec"] if mine["taken_sec"] is not None else 999999)).fetchone()[0]
        n = con.execute("SELECT COUNT(*) FROM daily_results WHERE day=?", (day.isoformat(),)).fetchone()[0]
    return int(better) + 1, int(n)


def calendar_grid(active_days: set[date], today_: date, weeks: int = 5) -> list[list[dict[str, Any]]]:
    """Weeks (Mon..Sun) ending with the current week; each cell has day, active, is_today, future."""
    start = today_ - timedelta(days=today_.weekday() + 7 * (weeks - 1))
    grid = []
    for w in range(weeks):
        grid.append([{"day": (d := start + timedelta(days=7 * w + i)), "active": d in active_days, "is_today": d == today_, "future": d > today_} for i in range(7)])
    return grid
