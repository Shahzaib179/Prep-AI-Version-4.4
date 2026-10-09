"""Habit engine: daily streak, daily goal, XP/levels, and 'questions I have already seen'.

Pure logic over the database (no Streamlit), so it is unit-tested without a browser.
Day boundaries use config.PROGRESS_TZ_OFFSET_MIN (default Pakistan, UTC+5) so a student practising at 11pm
local time is not counted on the wrong day.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

import config
from db_core import cached_read, connect

STREAK_MILESTONES = {3: ("streak_3", "3-day streak"), 7: ("streak_7", "7-day streak"), 14: ("streak_14", "14-day streak"), 30: ("streak_30", "30-day streak")}
LEVEL_XP = 50          # level n starts at 50 * (n-1)^2 XP  ->  L2=50, L3=200, L4=450, L5=800 ...


def local_today() -> date:
    return (datetime.utcnow() + timedelta(minutes=config.PROGRESS_TZ_OFFSET_MIN)).date()


# ------------------------------------------------------------------ XP and levels
def xp_for(answered: int, correct: int, quiz_done: bool = True, perfect: bool = False) -> int:
    xp = answered * config.XP_PER_ANSWER + correct * config.XP_PER_CORRECT
    if quiz_done:
        xp += config.XP_PER_QUIZ
    if perfect:
        xp += config.XP_PERFECT_BONUS
    return xp


def level_info(total_xp: int) -> dict[str, int]:
    level = int(math.isqrt(max(0, total_xp) // LEVEL_XP)) + 1
    start, nxt = LEVEL_XP * (level - 1) ** 2, LEVEL_XP * level ** 2
    return {"level": level, "xp_into_level": total_xp - start, "xp_for_next": nxt - start, "next_level_at": nxt}


def add_bonus_xp(student_id: str, xp: int, day: date | None = None) -> None:
    """Extra XP that does not count as answered questions (daily challenge bonus)."""
    day = (day or local_today()).isoformat()
    with connect() as con:
        con.execute("INSERT INTO daily_activity(student_id,day,questions,correct,quizzes,xp) VALUES(?,?,0,0,0,?) ON CONFLICT(student_id,day) DO UPDATE SET xp=daily_activity.xp+excluded.xp", (student_id, day, int(xp)))


# ------------------------------------------------------------------ activity log
def record_activity(student_id: str, answered: int, correct: int, perfect: bool = False, day: date | None = None, bonus_xp: int = 0) -> int:
    """Add one finished quiz to today's totals. Returns the XP earned."""
    day = day or local_today()
    xp = xp_for(answered, correct, True, perfect) + int(bonus_xp)
    with connect() as con:
        con.execute(
            "INSERT INTO daily_activity(student_id,day,questions,correct,quizzes,xp) VALUES(?,?,?,?,1,?) "
            "ON CONFLICT(student_id,day) DO UPDATE SET questions=daily_activity.questions+excluded.questions, "
            "correct=daily_activity.correct+excluded.correct, quizzes=daily_activity.quizzes+1, xp=daily_activity.xp+excluded.xp",
            (student_id, day.isoformat(), int(answered), int(correct), int(xp)),
        )
    return xp


@cached_read(30)
def _active_days(student_id: str) -> list[date]:
    with connect() as con:
        rows = con.execute("SELECT day FROM daily_activity WHERE student_id=? AND questions>0 ORDER BY day", (student_id,)).fetchall()
    return [date.fromisoformat(r[0]) for r in rows]


def streaks(days: list[date], today: date) -> tuple[int, int]:
    """(current, best). A streak is still alive if the student was active today OR yesterday (they have until midnight)."""
    if not days:
        return 0, 0
    ds = sorted(set(days))
    best = run = 1
    for a, b in zip(ds, ds[1:]):
        run = run + 1 if (b - a).days == 1 else 1
        best = max(best, run)
    last = ds[-1]
    if (today - last).days > 1:
        return 0, best
    cur = 1
    for i in range(len(ds) - 1, 0, -1):
        if (ds[i] - ds[i - 1]).days == 1:
            cur += 1
        else:
            break
    return cur, best


@cached_read(30)
def get_daily_goal(student_id: str) -> int:
    with connect() as con:
        row = con.execute("SELECT daily_goal FROM student_preferences WHERE student_id=?", (student_id,)).fetchone()
    return int(row[0]) if row and row[0] else config.DEFAULT_DAILY_GOAL


def set_daily_goal(student_id: str, goal: int) -> int:
    goal = max(5, min(200, int(goal)))
    with connect() as con:
        con.execute("UPDATE student_preferences SET daily_goal=? WHERE student_id=?", (goal, student_id))
    return goal


@cached_read(30)
def get_progress(student_id: str, today: date | None = None) -> dict[str, Any]:
    today = today or local_today()
    days = _active_days(student_id)
    current, best = streaks(days, today)
    with connect() as con:
        total_xp = int(con.execute("SELECT COALESCE(SUM(xp),0) FROM daily_activity WHERE student_id=?", (student_id,)).fetchone()[0])
        since = (today - timedelta(days=6)).isoformat()
        week_rows = {r[0]: int(r[1]) for r in con.execute("SELECT day, questions FROM daily_activity WHERE student_id=? AND day>=?", (student_id, since)).fetchall()}
    goal = get_daily_goal(student_id)
    week = [{"day": (today - timedelta(days=i)).isoformat(), "questions": week_rows.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(6, -1, -1)]
    today_q = week[-1]["questions"]
    return {
        "streak": current, "best_streak": best, "total_xp": total_xp, **level_info(total_xp),
        "goal": goal, "today_questions": today_q, "goal_done": today_q >= goal,
        "goal_pct": min(100.0, round(100 * today_q / goal, 1)) if goal else 0.0,
        "streak_at_risk": current > 0 and today_q == 0,       # active yesterday, nothing yet today
        "week": week, "active_days_total": len(set(days)),
    }


def new_milestones(student_id: str, before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str]]:
    """Achievements earned between two progress snapshots: (code, title) pairs."""
    out = []
    for n, (code, title) in STREAK_MILESTONES.items():
        if before["streak"] < n <= after["streak"]:
            out.append((code, title))
    if after["level"] > before["level"]:
        out.append((f"level_{after['level']}", f"Reached level {after['level']}"))
    if not before["goal_done"] and after["goal_done"]:
        out.append((f"goal_{local_today().isoformat()}", "Daily goal reached"))
    return out


# ------------------------------------------------------------------ questions already seen
def qhash(text: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def mark_seen(con: Any, student_id: str, questions: list[dict[str, Any]], subject: str, topic: str) -> None:
    now = datetime.utcnow().isoformat()
    for q in questions:
        text = str(q.get("question", "")).strip()
        if text:
            con.execute("INSERT INTO seen_questions(student_id,qhash,subject,topic,question_text,seen_at) VALUES(?,?,?,?,?,?) ON CONFLICT(student_id,qhash) DO NOTHING",
                        (student_id, qhash(text), subject, topic, text[:500], now))


def seen_hashes(student_id: str) -> set[str]:
    if not student_id:
        return set()
    with connect() as con:
        return {r[0] for r in con.execute("SELECT qhash FROM seen_questions WHERE student_id=?", (student_id,)).fetchall()}


def recent_seen_texts(student_id: str, subject: str, topic: str, limit: int = 12) -> list[str]:
    with connect() as con:
        rows = con.execute("SELECT question_text FROM seen_questions WHERE student_id=? AND subject=? AND topic=? ORDER BY seen_at DESC LIMIT ?", (student_id, subject, topic, limit)).fetchall()
    return [r[0] for r in rows]


def pick_fresh(candidates: list[dict[str, Any]], seen: set[str], already: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split candidates into (fresh, stale): fresh = never seen and not already chosen in this batch."""
    taken = {qhash(q.get("question", "")) for q in already}
    fresh, stale = [], []
    for q in candidates:
        h = qhash(q.get("question", ""))
        if h in taken:
            continue
        (stale if h in seen else fresh).append(q)
        taken.add(h)
    return fresh, stale


# ------------------------------------------------------------------ mistakes that can be re-asked
@cached_read(30)
def retakeable_mistakes(student_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Most recent wrong answers that stored their options, rebuilt as MCQs (newest first, no duplicates)."""
    with connect() as con:
        rows = con.execute("SELECT subject,topic,concept,question_text,correct_answer,explanation,options_json,difficulty FROM mistakes WHERE student_id=? AND options_json IS NOT NULL ORDER BY id DESC LIMIT ?", (student_id, limit * 4)).fetchall()
    with connect() as con:   # questions whose LATEST attempt was correct are fixed already: do not nag about them
        fixed = {qhash(r[0]) for r in con.execute(
            "SELECT question_text FROM question_attempts qa WHERE student_id=? AND is_correct=1 AND id=(SELECT MAX(id) FROM question_attempts WHERE student_id=qa.student_id AND question_text=qa.question_text)",
            (student_id,)).fetchall()}
    out, seen = [], set(fixed)
    for r in rows:
        h = qhash(r["question_text"])
        try:
            options = json.loads(r["options_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if h in seen or not options or r["correct_answer"] not in options:
            continue
        seen.add(h)
        out.append({"question": r["question_text"], "options": options, "answer": r["correct_answer"], "explanation": r["explanation"] or "",
                    "concept": r["concept"] or "", "difficulty": r["difficulty"] or "Medium", "subject": r["subject"], "topic": r["topic"]})
        if len(out) >= limit:
            break
    return out
