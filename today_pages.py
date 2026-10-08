"""'Today' page: streak, daily goal, XP, today's revision queue, and re-asking past mistakes.

Everything here uses stored data only (no AI call), so it works even when Groq is rate-limited.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

import streamlit as st

import charts
import progress
from adaptive import next_best_action
from db import due_revisions, revision_recommendations
from ui import hero


def progress_strip(student_id: str) -> dict[str, Any]:
    """Compact streak / level / goal row (used on Dashboard and Today)."""
    p = progress.get_progress(student_id)
    c = st.columns(4)
    c[0].metric("🔥 Streak", f"{p['streak']} day{'s' if p['streak'] != 1 else ''}", help=f"Best streak: {p['best_streak']} day(s). Practise at least once a day to keep it.")
    c[1].metric("⭐ Level", p["level"], help=f"{p['total_xp']} XP in total")
    c[2].metric("🎯 Today", f"{p['today_questions']} / {p['goal']}", help="Questions answered today vs. your daily goal")
    c[3].metric("XP to next level", p["xp_for_next"] - p["xp_into_level"])
    return p


def _week_label(day_iso: str) -> str:
    return date.fromisoformat(day_iso).strftime("%a")


def render_today(student_id: str, name: str, new_quiz: Callable[..., dict], render_quiz_taker: Callable[[str], None]) -> None:
    hero(f"📅 Today, {name}", "Your daily goal, streak and the most useful things to practise right now.")
    p = progress.get_progress(student_id)

    if p["goal_done"]:
        st.success(f"🎉 Daily goal reached ({p['today_questions']}/{p['goal']} questions). Anything more is bonus XP.")
    elif p["streak_at_risk"]:
        st.warning(f"🔥 Your {p['streak']}-day streak ends tonight. Answer {p['goal'] - p['today_questions']} more question(s) to keep it and hit today's goal.")
    elif p["streak"] == 0 and not p["active_days_total"]:
        st.info("Start your first streak: finish one quiz today.")

    progress_strip(student_id)

    g1, g2 = st.columns([1, 2])
    with g1:
        charts.show(charts.ring(p["goal_pct"], "Daily goal", color="#16A34A" if p["goal_done"] else None, center_text=f"{p['today_questions']}/{p['goal']}"))
        st.progress(min(1.0, p["xp_into_level"] / max(1, p["xp_for_next"])), text=f"Level {p['level']} · {p['xp_into_level']}/{p['xp_for_next']} XP")
    with g2:
        charts.show(charts.simple_bars([_week_label(d["day"]) for d in p["week"]], [d["questions"] for d in p["week"]], "Questions answered, last 7 days", "Questions", domain=None))

    with st.expander("⚙️ Change my daily goal"):
        with st.form("goal_form"):
            goal = st.number_input("Questions per day", 5, 200, int(p["goal"]), step=5, key="goal_input")
            if st.form_submit_button("Save goal"):
                progress.set_daily_goal(student_id, int(goal))
                st.rerun()
        st.caption("XP: 2 per answered question, +3 per correct answer, +10 per finished quiz, +20 for a perfect score.")

    action = next_best_action(student_id)
    st.markdown("### 🎯 Next best action")
    st.info(f"**{action['title']}** — {action['reason']}" + (f"  \nRecommended: **{action['subject']} → {action['topic']}**" if action.get("topic") else ""))

    st.markdown("### 🔄 Revision queue")
    due = due_revisions(student_id)
    if due:
        for x in due[:6]:
            st.warning(f"**{x['subject']} → {x['topic']}** · mastery {float(x.get('mastery') or 0):.0f}% · due now")
        st.caption("Open **Practice → Smart Revision** to generate a quiz for these topics.")
    else:
        recs = revision_recommendations(student_id, 3)
        if recs:
            st.success("Nothing is due today. Weakest topics worth a look: " + " · ".join(f"{r['subject']} → {r['topic']}" for r in recs))
        else:
            st.success("Nothing to revise yet. Finish a quiz to start your schedule.")

    st.markdown("### 🔁 Retake my mistakes")
    mistakes = progress.retakeable_mistakes(student_id, 20)
    active = st.session_state.get("quiz")
    if active and active.get("origin") == "retake":
        render_quiz_taker("quiz")
        return
    if not mistakes:
        st.info("No open mistakes to retake. Mistakes from new quizzes appear here until you answer them correctly.")
        return
    st.write(f"You have **{len(mistakes)}** question(s) you answered wrongly and have not fixed yet. No AI is needed for this quiz.")
    count = st.slider("Questions", 1, min(20, len(mistakes)), min(10, len(mistakes)), key="retake_count")
    if st.button("Start retake quiz", type="primary", key="retake_btn"):
        picked = mistakes[:count]
        subject = picked[0]["subject"] if len({q["subject"] for q in picked}) == 1 else "Mixed"
        topic = picked[0]["topic"] if len({q["topic"] for q in picked}) == 1 else "My mistakes"
        st.session_state.quiz = new_quiz([{k: v for k, v in q.items() if k not in ("subject", "topic")} for q in picked], subject, topic, "Medium", [], 0, origin="retake")
        st.rerun()
