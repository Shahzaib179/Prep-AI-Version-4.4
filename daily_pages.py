"""Daily Challenge screen: today's 5 questions, a streak calendar and a leaderboard."""
from __future__ import annotations

from typing import Any, Callable

import streamlit as st

import daily
import progress
from quiz_runtime import clock
from ui import hero

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def render_calendar(student_id: str, weeks: int = 5) -> None:
    today = daily.today()
    active = set(progress._active_days(student_id))
    grid = daily.calendar_grid(active, today, weeks)
    head = "| " + " | ".join(DAYS) + " |\n|" + "---|" * 7 + "\n"
    rows = ""
    for week in grid:
        cells = []
        for c in week:
            if c["future"]:
                cells.append("·")
            elif c["is_today"]:
                cells.append("🔥" if c["active"] else "🔲")
            else:
                cells.append("🟩" if c["active"] else "⬜")
        rows += "| " + " | ".join(cells) + " |\n"
    st.markdown(head + rows)
    st.caption("🟩 practised · ⬜ missed · 🔥 today done · 🔲 today still open")


def _leaderboard(student_id: str, day) -> None:
    board = daily.leaderboard(day, 10)
    if not board:
        st.info("Nobody has finished today's challenge yet. Be the first!")
        return
    st.dataframe([{"Rank": r["rank"], "Student": r["name"] + (" (you)" if r["student_id"] == student_id else ""), "Score": f"{r['correct']}/{r['total']}", "Time": clock(r["taken_sec"]) if r["taken_sec"] is not None else "—"} for r in board], hide_index=True, use_container_width=True)
    mine = daily.rank_of(student_id, day)
    if mine and mine[0] > len(board):
        st.caption(f"Your rank: {mine[0]} of {mine[1]}")


def render_daily(student_id: str, new_quiz: Callable[..., dict], render_quiz_taker: Callable[[str], None],
                 generate: Callable[[str, int], list[dict[str, Any]]] | None = None) -> None:
    hero("⚡ Daily Challenge", "Five questions, the same for every student, once a day. Finishing earns bonus XP and keeps your streak alive.")
    day = daily.today()
    p = progress.get_progress(student_id)
    c = st.columns(3)
    c[0].metric("🔥 Streak", f"{p['streak']} day{'s' if p['streak'] != 1 else ''}")
    c[1].metric("🏆 Best streak", p["best_streak"])
    c[2].metric("Practice days", p["active_days_total"])

    quiz = st.session_state.get("daily_quiz")
    if quiz and quiz.get("daily_day") != day.isoformat():
        st.session_state.daily_quiz = quiz = None          # a quiz left open overnight belongs to yesterday

    def settle() -> bool:
        """Once the quiz has a result, store it as today's single result (bonus XP) and refresh the screen."""
        q = st.session_state.get("daily_quiz")
        if q and q.get("result") and not q["result"].get("already") and not q["result"].get("timeout_only") and not daily.result_for(student_id, day):
            r = q["result"]
            daily.record_result(student_id, day, r["correct"], r["total"], r.get("taken"))
            return True
        return False

    mine = daily.result_for(student_id, day)
    if quiz and not mine:
        render_quiz_taker("daily_quiz")
        if settle():
            st.rerun()
    elif mine:
        st.success(f"✅ Today's challenge is done: **{mine['correct']}/{mine['total']}**" + (f" in {clock(mine['taken_sec'])}" if mine["taken_sec"] is not None else "") + f" · +{mine['xp']} bonus XP.")
        rk = daily.rank_of(student_id, day)
        if rk:
            st.caption(f"You are #{rk[0]} of {rk[1]} today. A new challenge appears at midnight.")
        if quiz and quiz.get("result"):
            with st.expander("Review my answers"):
                render_quiz_taker("daily_quiz")
    else:
        st.info("The challenge can be taken once per day, so finish it in one sitting. You have 5 minutes.")
        if st.button("▶ Start today's challenge", type="primary", key="daily_start"):
            with st.spinner("Preparing today's questions..."):
                shared = daily.get_challenge(day, generate)
            if not shared:
                st.error("Today's challenge is not available yet. Please try again in a minute.")
            else:
                qs = daily.for_student(shared, student_id, day)
                st.session_state.daily_quiz = new_quiz(qs, "Daily Challenge", day.isoformat(), "Mixed", [], 300, daily_day=day.isoformat(), origin="daily")
                st.rerun()

    st.markdown("### 📅 My streak calendar")
    render_calendar(student_id)
    st.markdown("### 🏆 Today's leaderboard")
    _leaderboard(student_id, day)
