"""Tutor tab 'AI Quality': results of the AI test set and the starter question pack."""
from __future__ import annotations

import streamlit as st

import ai_eval
import bank


def render_ai_quality(tutor_id: str) -> None:
    st.subheader("AI quality and starter questions")
    golden = ai_eval.load_golden()
    st.caption(f"The AI test set has **{len(golden)}** hand-checked questions and **{len(ai_eval.load_contexts())}** source passages. It measures how accurately the AI answers, how good the questions it writes are, and whether it admits when the sources do not contain an answer.")

    runs = ai_eval.recent_runs(8)
    if runs:
        latest = runs[0]
        st.markdown(f"**Latest run** · {str(latest['created_at'])[:16].replace('T', ' ')} · model `{latest['model']}`")
        cols = st.columns(max(1, len(latest["headline"])))
        for col, (name, score) in zip(cols, latest["headline"].items()):
            col.metric(name, f"{score}%")
        failed = [k for k, ok in latest["verdicts"].items() if not ok]
        (st.warning if failed else st.success)("Below target: " + ", ".join(failed) if failed else "All metrics meet their targets.")
        if len(runs) > 1:
            st.dataframe([{"When": str(r["created_at"])[:16].replace("T", " "), "Model": r["model"], **r["headline"]} for r in runs], hide_index=True, use_container_width=True)
    else:
        st.info("No AI test has been run yet. Run `python scripts/run_ai_eval.py --save` (see docs/V4.7_MONTH2.md); the results then appear here.")

    st.markdown("##### Starter question pack")
    st.caption("Add the checked questions to your own question bank (duplicates are skipped), then edit or use them in quizzes, mock tests and live quizzes.")
    subjects = sorted({q["subject"] for q in golden})
    chosen = st.multiselect("Subjects", subjects, default=subjects, key="aiq_subjects")
    if st.button("Add to my question bank", type="primary", key="aiq_import"):
        res = bank.bulk_add(tutor_id, [q for q in golden if q["subject"] in chosen], source="manual")
        st.success(f"Added {res['added']} question(s); {res['duplicates']} already in your bank.")
    with st.expander("Browse the set"):
        st.dataframe([{"ID": q["id"], "Subject": q["subject"], "Topic": q["topic"], "Level": q["difficulty"], "Question": q["question"], "Answer": f"{q['answer']}. {q['options'][q['answer']]}"} for q in golden if q["subject"] in chosen], hide_index=True, use_container_width=True)
