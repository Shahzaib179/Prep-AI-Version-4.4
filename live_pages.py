"""Live quiz screens: host console (tutor) and player screen (student). Polling fragments refresh every 2 seconds."""
from __future__ import annotations

import csv
import io
from typing import Any

import streamlit as st

import bank
import live
from ui import hero

POLL_SEC = 2


def _csv(report: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "student_id", "name", "score", "correct", "total", "accuracy_pct"])
    for i, r in enumerate(report, start=1):
        w.writerow([i, r["student_id"], r["name"], r["score"], r["correct"], r["total"], r["accuracy"]])
    return buf.getvalue()


def _bars(counts: dict[str, int], answer: str) -> None:
    total = sum(counts.values()) or 1
    for k in "ABCD":
        n = counts.get(k, 0)
        st.progress(n / total, text=f"{'✅ ' if k == answer else ''}{k}: {n}")


def _standings_table(session_id: int, me: str | None = None, limit: int = 5) -> None:
    rows = live.standings(session_id, limit)
    if rows:
        st.dataframe([{"Rank": r["rank"], "Player": r["name"] + (" (you)" if r["student_id"] == me else ""), "Points": r["score"]} for r in rows], hide_index=True, use_container_width=True)


# ------------------------------------------------------------------ host (tutor)
def render_host(host_id: str, sources: dict[str, Any] | None = None) -> None:
    st.subheader("Live quiz")
    st.caption("Run a quiz in real time: students join with a code, everyone sees each question at the same moment, faster correct answers earn more points, and you see a live scoreboard.")
    code = st.session_state.get("live_host_code")
    if code:
        _host_console(host_id, code)
        return

    quiz = (sources or {}).get("quiz")
    mode_options = ["From my question bank"] + (["From the quiz I just created"] if quiz else [])
    mode = st.radio("Questions", mode_options, horizontal=True, key="lv_mode")
    questions: list[dict[str, Any]] = []
    title = "Live quiz"
    if mode == "From my question bank":
        counts = bank.subject_counts(host_id)
        if not counts:
            st.info("Your question bank is empty. Add questions in the **Question Bank** tab first, or create a quiz and use it here.")
        else:
            subject = st.selectbox("Subject", ["All"] + [s for s in counts if s != "Unsorted"], key="lv_subject")
            n = st.slider("Number of questions", 3, 30, 10, key="lv_n")
            questions = bank.pick_questions(host_id, n, None if subject == "All" else subject)
            title = f"Live: {subject}" if subject != "All" else "Live quiz"
            st.caption(f"{len(questions)} question(s) will be used.")
    else:
        questions = list(quiz["questions"])
        title = f"Live: {quiz.get('topic') or quiz.get('subject') or 'quiz'}"
        st.caption(f"{len(questions)} question(s) from your current quiz.")
    secs = st.slider("Seconds per question", 10, 90, 20, step=5, key="lv_secs")
    if st.button("Create live quiz", type="primary", key="lv_create", disabled=not questions):
        try:
            made = live.create_session(host_id, title, questions, secs)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state.live_host_code = made["code"]
            st.rerun()

    past = live.host_sessions(host_id, 5)
    if past:
        st.markdown("##### My recent live quizzes")
        for p in past:
            c1, c2 = st.columns([4, 1])
            c1.write(f"**{p['title']}** · code `{p['code']}` · {p['players']} player(s) · {p['state']}")
            if c2.button("Open", key=f"lv_open_{p['code']}"):
                st.session_state.live_host_code = p["code"]
                st.rerun()


@st.fragment(run_every=POLL_SEC)
def _host_console(host_id: str, code: str) -> None:
    row = live.get_state(code)
    if not row or row["host_id"] != host_id:
        st.error("Live quiz not found.")
        st.session_state.live_host_code = None
        return
    sid = row["id"]
    questions = live.get_questions(sid)
    ph = live.phase(row)
    st.markdown(f"## Join code: `{row['code']}`")
    st.caption(f"{row['title']} · {len(questions)} questions · {row['seconds_per_q']} s each. Students open **Live Quiz** in their sidebar and type the code.")
    ps = live.players(sid)

    if ph == live.LOBBY:
        st.info(f"Waiting room: **{len(ps)}** student(s) joined" + (": " + ", ".join(p["name"] for p in ps[:30]) if ps else "."))
        if st.button("▶ Start the quiz", type="primary", key="lv_start", disabled=not ps):
            live.advance(code, host_id, expected_q_index=-1)
            st.rerun(scope="fragment")
    elif ph in (live.QUESTION, live.REVEAL):
        qi = row["q_index"]
        q = questions[qi]
        st.markdown(f"### Question {qi + 1} of {len(questions)}")
        st.markdown(f"**{q['question']}**")
        for k in "ABCD":
            st.write(f"{k}. {q['options'][k]}")
        answered = live.answered_count(sid, qi)
        if ph == live.QUESTION:
            left = live.seconds_left(row)
            st.progress(min(1.0, left / row["seconds_per_q"]), text=f"⏱ {int(left) + 1 if left else 0} s left · {answered}/{len(ps)} answered")
            c1, c2 = st.columns(2)
            if c1.button("Show answer now", key=f"lv_rev_{qi}"):
                live.reveal(code, host_id)
                st.rerun(scope="fragment")
        else:
            st.success(f"Correct answer: **{q['answer']}. {q['options'][q['answer']]}**")
            if q.get("explanation"):
                st.caption(q["explanation"])
            _bars(live.answer_counts(sid, qi), q["answer"])
            st.markdown("**Top players**")
            _standings_table(sid)
            last = qi + 1 >= len(questions)
            if st.button("🏁 Finish quiz" if last else "Next question ▶", type="primary", key=f"lv_next_{qi}"):
                live.advance(code, host_id, expected_q_index=qi)
                st.rerun(scope="fragment")
        if st.button("End quiz now", key="lv_end"):
            live.end_session(code, host_id)
            st.rerun(scope="fragment")
    else:
        st.success("The quiz has finished.")
        report = live.final_report(sid)
        if report:
            st.dataframe([{"Rank": i, "Student": r["name"], "Points": r["score"], "Correct": f"{r['correct']}/{r['total']}", "Accuracy %": r["accuracy"]} for i, r in enumerate(report, start=1)], hide_index=True, use_container_width=True)
            st.download_button("Download results (CSV)", _csv(report), file_name=f"live_{row['code']}.csv", mime="text/csv", key="lv_csv")
        else:
            st.info("Nobody joined.")
        if st.button("Close and create another", key="lv_close"):
            st.session_state.live_host_code = None
            st.rerun()


# ------------------------------------------------------------------ player (student)
def render_player(student_id: str, name: str) -> None:
    hero("🎮 Live Quiz", "Your tutor runs a quiz in real time. Enter the code, wait for the start, and answer fast: speed earns points.")
    code = st.session_state.get("live_code")
    if code:
        _player_screen(student_id, name, code)
        return
    with st.form("lv_join"):
        entered = st.text_input("Live quiz code", placeholder="e.g. K7M2QX", max_chars=12)
        go = st.form_submit_button("Join", type="primary")
    if go:
        ok, msg, row = live.join(entered, student_id, name)
        if ok:
            st.session_state.live_code = row["code"]
            st.rerun()
        st.error(msg)


@st.fragment(run_every=POLL_SEC)
def _player_screen(student_id: str, name: str, code: str) -> None:
    row = live.get_state(code)
    if not row:
        st.session_state.live_code = None
        st.error("This live quiz no longer exists.")
        return
    sid = row["id"]
    ph = live.phase(row)
    st.caption(f"**{row['title']}** · code `{row['code']}`")
    if ph == live.LOBBY:
        st.info(f"You are in, {name}. Waiting for the tutor to start... ({len(live.players(sid))} joined)")
    elif ph in (live.QUESTION, live.REVEAL):
        qi = row["q_index"]
        cache = st.session_state.setdefault("live_q_cache", {})
        if sid not in cache:
            cache[sid] = live.get_questions(sid)
        questions = cache[sid]
        q = questions[qi]
        shown = live.display_question(q, student_id, sid)
        mine = live.my_answer(sid, student_id, qi)
        st.markdown(f"### Question {qi + 1} of {len(questions)}")
        st.markdown(f"**{q['question']}**")
        if ph == live.QUESTION:
            left = live.seconds_left(row)
            st.progress(min(1.0, left / row["seconds_per_q"]), text=f"⏱ {int(left) + 1 if left else 0} s")
            if mine:
                st.success("Answer locked in. Waiting for the others...")
            else:
                for k in "ABCD":
                    if st.button(f"{k}.  {shown['options'][k]}", key=f"lvp_{sid}_{qi}_{k}", use_container_width=True):
                        res = live.submit_answer(sid, student_id, qi, live.original_letter(q, shown, k))
                        if not res["accepted"]:
                            st.warning(res["reason"])
                        st.rerun(scope="fragment")
        else:
            if mine is None:
                st.warning("You did not answer this question.")
            elif mine["is_correct"]:
                st.success(f"✅ Correct! +{mine['points']} points")
            else:
                st.error(f"❌ Not this time. Correct answer: **{q['answer']}. {q['options'][q['answer']]}**")
            if q.get("explanation"):
                st.caption(q["explanation"])
            rk = live.my_rank(sid, student_id)
            if rk:
                st.markdown(f"**Your rank: {rk[0]} of {rk[1]}**")
            _standings_table(sid, student_id)
            st.caption("Waiting for the next question...")
    else:
        done = st.session_state.setdefault("live_credited", set())
        if sid not in done:
            live.finish_credit(sid, student_id)
            done.add(sid)
        rk = live.my_rank(sid, student_id)
        st.success("🏁 The live quiz is over!" + (f" You finished **#{rk[0]}** of {rk[1]}." if rk else ""))
        _standings_table(sid, student_id, 10)
        st.caption("Your answers were added to your progress, mistakes and streak.")
        if st.button("Leave", key="lv_leave"):
            st.session_state.live_code = None
            st.rerun()
