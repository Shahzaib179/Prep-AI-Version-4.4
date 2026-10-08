"""Mock test screens: set up, take (sectioned, timed, resumable), results and history."""
from __future__ import annotations

import time
from typing import Any, Callable

import streamlit as st

import bank
import charts
import mock
from quiz_runtime import clock, is_expired, minutes_to_seconds
from ui import hero

CUSTOM = "Custom"
SUBJECTS = mock.SECTION_ORDER


def _setup(student_id: str, new_quiz: Callable[..., dict], generate: Callable[[str, int], list[dict[str, Any]]]) -> None:
    names = list(mock.BLUEPRINTS) + [CUSTOM]
    pick = st.selectbox("Mock test type", names, key="mk_type")
    if pick == CUSTOM:
        cols = st.columns(len(SUBJECTS))
        sections = [(s, int(cols[i].number_input(s, 0, 100, 10 if i < 3 else 0, key=f"mk_c_{s}"))) for i, s in enumerate(SUBJECTS)]
        default_min, negative_default = mock.minutes_for(sum(c for _, c in sections)), False
    else:
        bp = mock.BLUEPRINTS[pick]
        sections, default_min, negative_default = bp["sections"], bp["minutes"], bp["negative"]
    total = sum(c for _, c in sections)

    c1, c2, c3 = st.columns(3)
    minutes = c1.number_input("Time limit (minutes)", 1, 600, max(1, default_min), key=f"mk_min_{pick}_{total}")
    difficulty = c2.selectbox("Difficulty", ["Any", "Easy", "Medium", "Hard"], key="mk_diff")
    negative = c3.checkbox("Negative marking (−0.25)", value=negative_default, key="mk_neg", help="The published MDCAT pattern has no negative marking; switch it on for stricter practice.")

    st.markdown("**Questions available in your bank**")
    rows = []
    for s, want in sections:
        have = bank.available_for_student(student_id, s)
        rows.append({"Section": s, "Needed": want, "In bank": have, "Short by": max(0, want - have)})
    st.dataframe(rows, hide_index=True)
    short = sum(r["Short by"] for r in rows)
    use_ai = False
    max_ai = 0
    if short:
        use_ai = st.checkbox(f"Fill the {short} missing question(s) with AI", value=True, key="mk_ai", help="AI questions are saved to your bank, so the next mock needs fewer.")
        if use_ai:
            max_ai = st.slider("Maximum AI-generated questions", 5, 200, min(short, 100), key="mk_ai_max")
            st.caption("Generating many questions takes a few minutes and uses your AI quota. A smaller bank-only mock starts instantly.")

    if st.button("Build my mock test", type="primary", key="mk_build", disabled=total == 0):
        with st.spinner("Preparing your mock test..."):
            built = mock.assemble(student_id, sections, None if difficulty == "Any" else difficulty, generate=generate if use_ai else None, max_ai=max_ai)
        questions = built["questions"]
        if not questions:
            st.error("No questions could be prepared. Add questions to your bank or switch on AI fill.")
            return
        miss = {s: r["missing"] for s, r in built["report"].items() if r["missing"]}
        if miss:
            st.warning("Some sections are smaller than planned: " + ", ".join(f"{s} −{m}" for s, m in miss.items()))
        quiz = new_quiz(questions, "Mock Test", pick if pick != CUSTOM else "Custom mock", "Mixed", [], minutes_to_seconds(minutes), negative=negative, blueprint=pick, mock=True)
        st.session_state.mock = quiz
        mock.save_progress(student_id, quiz)
        st.rerun()


def _taker(student_id: str, quiz: dict, submit_quiz: Callable[[str, bool], None], timer: Callable[[str], None]) -> None:
    questions, limit, uid = quiz["questions"], quiz["time_limit_sec"], quiz["uid"]
    sections = mock.section_names(questions)
    st.write(f"**{quiz['topic']}** · {len(questions)} questions · ⏱ {clock(limit)} · " + ("negative marking −0.25" if quiz.get("negative") else "no negative marking"))

    if quiz["started_at"] is None:
        st.info("The clock starts when you press Start and **keeps running even if you close the page**. Your answers are saved automatically, so you can come back and continue.")
        c1, c2 = st.columns(2)
        if c1.button("▶ Start mock test", type="primary", key=f"mk_start_{uid}"):
            quiz["started_at"] = time.time()
            mock.save_progress(student_id, quiz)
            st.rerun()
        if c2.button("Cancel this mock", key=f"mk_cancel_{uid}"):
            mock.clear_progress(student_id)
            st.session_state.mock = None
            st.rerun()
        return

    expired = is_expired(quiz["started_at"], limit)
    if not expired:
        timer("mock")

    answers, stamps = quiz["answers"], quiz["answer_ts"]
    before = dict(answers)
    tabs = st.tabs([f"{s} ({sum(1 for i, q in enumerate(questions) if q.get('section') == s and i in answers)}/{sum(1 for q in questions if q.get('section') == s)})" for s in sections])
    for tab, section in zip(tabs, sections):
        with tab:
            for i, q in enumerate(questions):
                if q.get("section") != section:
                    continue
                st.markdown(f"**Q{i + 1}.** {q.get('question', '')}")
                options = q.get("options") or {}
                choice = st.radio("Answer", list(options), format_func=lambda k, q=q: f"{k}. {q['options'][k]}", key=f"mkq_{uid}_{i}", index=(list(options).index(answers[i]) if answers.get(i) in options else None), label_visibility="collapsed")
                if choice and answers.get(i) != choice:
                    answers[i] = choice
                    stamps[i] = time.time()
    if answers != before:
        mock.save_progress(student_id, quiz)

    st.progress(len(answers) / len(questions) if questions else 0.0, text=f"{len(answers)} of {len(questions)} answered")
    clicked = st.button("Submit mock test", type="primary", key=f"mk_submit_{uid}")
    expired = expired or is_expired(quiz["started_at"], limit)
    if clicked or expired:
        submit_quiz("mock", expired)
        if quiz.get("result"):
            _finish(student_id, quiz)
            st.rerun()


def _finish(student_id: str, quiz: dict) -> None:
    """Store the section breakdown once and drop the saved in-progress copy."""
    if quiz.get("_mock_saved") or not quiz.get("result") or quiz["result"].get("already"):
        return
    scores = mock.section_scores(quiz["questions"], quiz["result"]["answers"], bool(quiz.get("negative")))
    quiz["scores"] = scores
    mock.save_result(student_id, quiz["result"].get("quiz_id"), quiz.get("blueprint", "Mock test"), scores, quiz["result"].get("taken"))
    mock.clear_progress(student_id)
    quiz["_mock_saved"] = True


def _result(student_id: str, quiz: dict) -> None:
    _finish(student_id, quiz)
    r = quiz["result"]
    scores = quiz.get("scores") or mock.section_scores(quiz["questions"], r["answers"], bool(quiz.get("negative")))
    if r.get("timed_out"):
        st.warning("⏰ Time was up: your mock test was submitted automatically with the answers you had selected.")
    st.markdown("## Mock test result")
    c = st.columns(4)
    c[0].metric("Marks", f"{scores['marks']:g} / {scores['max_marks']:g}")
    c[1].metric("Percentage", f"{scores['percent']:.1f}%")
    c[2].metric("Correct / Wrong / Skipped", f"{scores['correct']} / {scores['wrong']} / {scores['skipped']}")
    c[3].metric("Time used", clock(r.get("taken")))
    if r.get("xp"):
        st.success(f"⭐ +{r['xp']} XP · 🔥 {r.get('streak', 0)}-day streak")
    rows = scores["sections"]
    charts.show(charts.simple_bars([x["section"] for x in rows], [x["percent"] for x in rows], "Score by section", "% of section"))
    st.dataframe([{"Section": x["section"], "Questions": x["total"], "Correct": x["correct"], "Wrong": x["wrong"], "Skipped": x["skipped"], "Marks": x["marks"], "Accuracy of attempted %": x["accuracy"]} for x in rows], hide_index=True)
    for tip in mock.focus_advice(scores):
        st.info(tip)
    st.markdown("### Review")
    for section in mock.section_names(quiz["questions"]):
        wrong = [(i, q) for i, q in enumerate(quiz["questions"]) if q.get("section") == section and r["answers"].get(i) != str(q.get("answer", "")).strip()]
        with st.expander(f"{section}: {len(wrong)} to review"):
            for i, q in wrong:
                chosen = r["answers"].get(i)
                st.markdown(f"**Q{i + 1}.** {q.get('question', '')}")
                st.write(f"Your answer: **{chosen or 'Skipped'}** · Correct: **{q.get('answer')}** — {q.get('options', {}).get(str(q.get('answer')), '')}")
                if q.get("explanation"):
                    st.caption(q["explanation"])
    if st.button("Start another mock test", key="mk_again"):
        st.session_state.mock = None
        st.rerun()


def _history(student_id: str) -> None:
    hist = mock.history(student_id)
    if not hist:
        st.info("Your finished mock tests will appear here with a trend line.")
        return
    charts.show(charts.score_trend([{"score": h["percent"], "topic": h["blueprint"], "created_at": h["created_at"]} for h in hist], "Mock test score over time"))
    st.dataframe([{"When": str(h["created_at"])[:16].replace("T", " "), "Type": h["blueprint"], "Marks": f"{h['marks']:g}/{h['max_marks']:g}", "%": h["percent"],
                   "Correct": h["correct"], "Wrong": h["wrong"], "Skipped": h["skipped"], "Time": clock(h["taken_sec"]) if h["taken_sec"] is not None else "—"} for h in reversed(hist)], hide_index=True)


def render_mock(student_id: str, new_quiz: Callable[..., dict], submit_quiz: Callable[[str, bool], None], timer: Callable[[str], None],
                generate: Callable[[str, int], list[dict[str, Any]]]) -> None:
    hero("🧪 Mock Test", "Full-length sectioned practice exams. Answers are saved as you go, so a refresh never loses your progress.")
    tab_take, tab_hist = st.tabs(["Take a mock test", "My mock results"])
    with tab_hist:
        _history(student_id)
    with tab_take:
        quiz = st.session_state.get("mock")
        if quiz and quiz.get("result"):
            _result(student_id, quiz)
            return
        if quiz:
            _taker(student_id, quiz, submit_quiz, timer)
            return
        saved = mock.load_progress(student_id)
        if saved:
            left = None if saved["started_at"] is None else int(saved["started_at"] + saved["time_limit_sec"] - time.time())
            st.warning("You have an unfinished mock test." + ("" if left is None else (f" Time left: **{clock(left)}**." if left > 0 else " Its time has run out; open it to submit your saved answers.")))
            c1, c2 = st.columns(2)
            if c1.button("▶ Resume it", type="primary", key="mk_resume"):
                st.session_state.mock = saved
                st.rerun()
            if c2.button("Discard it", key="mk_discard"):
                mock.clear_progress(student_id)
                st.rerun()
            return
        _setup(student_id, new_quiz, generate)
