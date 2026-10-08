"""Tutor UI: edit quiz questions, manage the question bank (add / edit / delete / CSV in-out / build quiz)."""
from __future__ import annotations

from typing import Any, Callable

import streamlit as st

import bank
import question_tools as qt

SUBJECTS = ["Biology", "Chemistry", "Physics", "English", "Logical Reasoning"]
PAGE = 10


def _q_fields(prefix: str, q: dict[str, Any], delete_option: bool = False) -> dict[str, Any]:
    """Widgets for one question. Returns the edited values (call inside a form)."""
    text = st.text_area("Question", q.get("question", ""), key=f"{prefix}_q")
    cols = st.columns(2)
    opts = q.get("options") or {}
    values = {}
    for i, letter in enumerate(qt.LETTERS):
        values[letter] = cols[i % 2].text_input(f"Option {letter}", opts.get(letter, ""), key=f"{prefix}_{letter}")
    c1, c2 = st.columns(2)
    answer = c1.selectbox("Correct answer", qt.LETTERS, index=qt.LETTERS.index(q.get("answer")) if q.get("answer") in qt.LETTERS else 0, key=f"{prefix}_ans")
    diff = c2.selectbox("Difficulty", qt.DIFFICULTIES, index=qt.DIFFICULTIES.index(q.get("difficulty")) if q.get("difficulty") in qt.DIFFICULTIES else 1, key=f"{prefix}_diff")
    expl = st.text_area("Explanation (shown to students after submitting)", q.get("explanation", ""), key=f"{prefix}_ex")
    out = {"question": text, "options": values, "answer": answer, "difficulty": diff, "explanation": expl, "concept": q.get("concept", "")}
    out["_delete"] = bool(st.checkbox("Remove this question from the quiz", key=f"{prefix}_del")) if delete_option else False
    return out


def render_question_editor(quiz: dict[str, Any]) -> None:
    """Edit / remove the questions of the quiz being prepared (before it is published)."""
    ver = quiz.setdefault("editor_version", 0)
    uid = f"{quiz['uid']}_{ver}"
    if quiz.get("shared_code") or quiz.get("share_code_created"):
        st.info("A published quiz cannot be edited. Edit your questions first, then publish.")
        return
    st.caption("Fix wording, options or the correct answer, or remove weak questions. Nothing changes until you press **Apply edits**.")
    with st.form(f"editor_{uid}"):
        edited = []
        for i, q in enumerate(quiz["questions"]):
            with st.expander(f"Q{i + 1}. {str(q.get('question', ''))[:90]}"):
                edited.append(_q_fields(f"ed_{uid}_{i}", q, delete_option=True))
        apply = st.form_submit_button("Apply edits", type="primary")
    if not apply:
        return
    kept, problems = [], []
    for i, (old, new) in enumerate(zip(quiz["questions"], edited), start=1):
        if new.pop("_delete"):
            continue
        errs = qt.validate_question(new)
        if errs:
            problems.append(f"Q{i}: " + " ".join(errs))
            kept.append(old)                         # keep the previous valid version
        else:
            n = qt.normalize_question(new)
            kept.append({**old, **n})
    if not kept:
        st.error("A quiz needs at least one question.")
        return
    quiz["questions"] = kept
    quiz["editor_version"] = ver + 1
    for p in problems:
        st.warning(p + " (not applied)")
    if not problems:
        st.success(f"Edits applied. The quiz now has {len(kept)} question(s).")
        st.rerun()


def render_save_to_bank(quiz: dict[str, Any], tutor_id: str) -> None:
    if st.button("💾 Save these questions to my question bank", key=f"tobank_{quiz['uid']}"):
        r = bank.bulk_add(tutor_id, quiz["questions"], quiz.get("subject", ""), quiz.get("topic", ""), source="quiz")
        st.success(f"Saved {r['added']} new question(s); {r['duplicates']} were already in your bank." + (f" {len(r['invalid'])} invalid skipped." if r["invalid"] else ""))


def render_add_from_bank(quiz: dict[str, Any], tutor_id: str) -> None:
    with st.expander("➕ Add questions from my question bank"):
        subject = st.selectbox("Subject", SUBJECTS, key=f"afb_subject_{quiz['uid']}")
        have = bank.count_questions(tutor_id, subject=subject)
        if not have:
            st.info(f"No {subject} questions in your bank yet.")
            return
        n = st.slider("How many random questions", 1, min(50, have), min(5, have), key=f"afb_n_{quiz['uid']}")
        if st.button("Add them", key=f"afb_btn_{quiz['uid']}"):
            have_text = {q["question"].strip().lower() for q in quiz["questions"]}
            picked = [q for q in bank.pick_questions(tutor_id, n + len(quiz["questions"]), subject=subject) if q["question"].strip().lower() not in have_text][:n]
            quiz["questions"] = quiz["questions"] + [{k: q[k] for k in ("question", "options", "answer", "explanation", "concept", "difficulty")} for q in picked]
            quiz["editor_version"] = quiz.get("editor_version", 0) + 1
            st.success(f"Added {len(picked)} question(s).")
            st.rerun()


def render_bank_manager(tutor_id: str, new_quiz: Callable[..., dict]) -> None:
    st.subheader("🗂 Question bank")
    counts = bank.subject_counts(tutor_id)
    total = sum(counts.values())
    st.caption(f"{total} question(s) in your private bank" + (": " + " · ".join(f"{k} {v}" for k, v in counts.items()) if counts else ". Add some below, import a CSV, or save a generated quiz to the bank."))

    tabs = st.tabs(["Browse & edit", "Add a question", "Import / export CSV", "Make a quiz from the bank"])

    with tabs[0]:
        c = st.columns(4)
        subject = c[0].selectbox("Subject", ["All"] + SUBJECTS, key="bk_subject")
        topic = c[1].text_input("Topic contains", key="bk_topic")
        diff = c[2].selectbox("Difficulty", ["All"] + qt.DIFFICULTIES, key="bk_diff")
        search = c[3].text_input("Search text", key="bk_search")
        flt = dict(subject=None if subject == "All" else subject, topic=topic.strip() or None, difficulty=None if diff == "All" else diff, search=search.strip() or None)
        n = bank.count_questions(tutor_id, **flt)
        pages = max(1, (n + PAGE - 1) // PAGE)
        page = int(st.number_input("Page", 1, pages, 1, key="bk_page")) if pages > 1 else 1
        st.caption(f"{n} matching question(s) · page {page} of {pages}")
        for row in bank.list_questions(tutor_id, limit=PAGE, offset=(page - 1) * PAGE, **flt):
            flag = " · 🌐 shared with students' mock tests" if row["is_shared"] else ""
            with st.expander(f"[{row['subject'] or '—'} → {row['topic'] or '—'}] {row['question'][:80]}{flag}"):
                with st.form(f"bk_form_{row['id']}"):
                    v = _q_fields(f"bk_{row['id']}", row)
                    c1, c2 = st.columns(2)
                    sub = c1.selectbox("Subject", SUBJECTS, index=SUBJECTS.index(row["subject"]) if row["subject"] in SUBJECTS else 0, key=f"bk_{row['id']}_sub")
                    top = c2.text_input("Topic", row["topic"], key=f"bk_{row['id']}_top")
                    shared = st.checkbox("Share with students' mock tests", row["is_shared"], key=f"bk_{row['id']}_sh", help="Students can draw this question into their own mock tests. They never see your other questions.")
                    b1, b2 = st.columns(2)
                    save = b1.form_submit_button("Save changes", type="primary")
                    remove = b2.form_submit_button("Delete")
                if save:
                    ok, msg = bank.update_question(tutor_id, row["id"], v, sub, top.strip(), shared)
                    (st.success if ok else st.error)(msg)
                    if ok:
                        st.rerun()
                if remove:
                    bank.delete_questions(tutor_id, [row["id"]])
                    st.rerun()

    with tabs[1]:
        with st.form("bk_add_form", clear_on_submit=False):
            c1, c2 = st.columns(2)
            sub = c1.selectbox("Subject", SUBJECTS, key="bk_new_sub")
            top = c2.text_input("Topic", key="bk_new_top")
            v = _q_fields("bk_new", {"options": {}, "answer": "A"})
            shared = st.checkbox("Share with students' mock tests", False, key="bk_new_sh")
            go = st.form_submit_button("Add to bank", type="primary")
        if go:
            _, status = bank.add_question(tutor_id, v, sub, top.strip(), "manual", shared)
            if status == "added":
                st.success("Added to your bank.")
            elif status == "duplicate":
                st.warning("A question with this exact text is already in your bank.")
            else:
                st.error(status.replace("invalid: ", ""))

    with tabs[2]:
        st.markdown("**Import** a CSV with columns `question, A, B, C, D, answer` (optional: `subject, topic, difficulty, explanation, concept`).")
        up = st.file_uploader("CSV file", type=["csv"], key="bk_csv_up")
        d1, d2 = st.columns(2)
        dsub = d1.selectbox("Default subject (if the file has no subject column)", SUBJECTS, key="bk_csv_sub")
        dtop = d2.text_input("Default topic", key="bk_csv_top")
        if up is not None and st.button("Import file", type="primary", key="bk_csv_go"):
            good, errors = qt.parse_csv(up.getvalue().decode("utf-8-sig", errors="replace"), dsub, dtop)
            r = bank.bulk_add(tutor_id, good, dsub, dtop, source="csv")
            st.success(f"Imported {r['added']} question(s); {r['duplicates']} duplicate(s) skipped.")
            for e in (errors + r["invalid"])[:15]:
                st.warning(e)
        st.divider()
        sample = qt.to_csv([{"subject": "Biology", "topic": "Cells", "difficulty": "Easy", "question": "Which organelle produces most of the cell's ATP?",
                             "options": {"A": "Mitochondrion", "B": "Ribosome", "C": "Golgi body", "D": "Lysosome"}, "answer": "A", "explanation": "Oxidative phosphorylation happens in mitochondria.", "concept": "Organelles"}])
        st.download_button("📄 Download a sample CSV", sample, "question_bank_sample.csv", "text/csv", key="bk_sample")
        if total:
            st.download_button("📥 Export my whole bank (CSV)", qt.to_csv(bank.export_rows(tutor_id)), "my_question_bank.csv", "text/csv", key="bk_export")

    with tabs[3]:
        c = st.columns(3)
        subject = c[0].selectbox("Subject", SUBJECTS, key="bq_subject")
        topic = c[1].text_input("Topic contains (optional)", key="bq_topic")
        diff = c[2].selectbox("Difficulty", ["Any"] + qt.DIFFICULTIES, key="bq_diff")
        have = bank.count_questions(tutor_id, subject=subject, topic=topic.strip() or None, difficulty=None if diff == "Any" else diff)
        if not have:
            st.info("No matching questions in your bank.")
        else:
            n = st.slider("Number of questions", 1, min(100, have), min(10, have), key="bq_n")
            if st.button("Build quiz", type="primary", key="bq_go"):
                picked = bank.pick_questions(tutor_id, n, subject=subject, topic=topic.strip() or None, difficulty=None if diff == "Any" else diff)
                qs = [{k: q[k] for k in ("question", "options", "answer", "explanation", "concept", "difficulty")} for q in picked]
                st.session_state.quiz = new_quiz(qs, subject, topic.strip() or "Mixed", "Mixed" if diff == "Any" else diff, [], 0)
                bank.mark_used([q["id"] for q in picked])
                st.success(f"Quiz built from {len(qs)} bank question(s). Open the **Publish** tab to edit and publish it.")
