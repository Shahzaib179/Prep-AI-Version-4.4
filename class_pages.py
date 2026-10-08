"""Class screens: tutors manage classes and deadlines, students join classes and see what is due."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

import streamlit as st

import classes
from db import list_shared_quizzes
from ui import hero

STATUS_LABEL = {"done": "✅ Done", "done_late": "🟠 Done late", "open": "🟦 Open", "due_soon": "🟡 Due soon",
                "overdue": "🔴 Overdue (late allowed)", "missed": "⛔ Missed"}


def _local_now() -> datetime:
    return classes.utc_iso_to_local(classes.now_utc().isoformat(timespec="minutes")) or datetime.utcnow()


def report_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["student_id", "name", "status", "score", "submitted_at", "time_taken_sec"])
    for r in report["rows"]:
        w.writerow([r["student_id"], r["name"], r["status"], r["score"] if r["score"] is not None else "", r["submitted_at"] or "", r["time_taken_sec"] or ""])
    return buf.getvalue()


# ------------------------------------------------------------------ tutor
def render_tutor_classes(tutor_id: str) -> None:
    st.subheader("Classes and deadlines")
    st.caption("Create a class, give students its code, then assign a published quiz with a deadline. Students must START the quiz before the deadline.")
    with st.form("cls_new", clear_on_submit=True):
        name = st.text_input("New class name", placeholder="e.g. MDCAT 2027 – Morning batch", max_chars=80)
        if st.form_submit_button("Create class", type="primary"):
            try:
                c = classes.create_class(tutor_id, name)
                st.success(f"Class created. Share this code with students: **{c['join_code']}**")
            except ValueError as exc:
                st.error(str(exc))

    show_archived = st.checkbox("Show archived classes", key="cls_archived")
    items = classes.list_classes(tutor_id, include_archived=show_archived)
    if not items:
        st.info("No classes yet. Create one above.")
        return
    for c in items:
        label = f"{c['name']} · code {c['join_code']} · {c['members']} student(s) · {c['assignments']} assignment(s)" + ("" if c["is_active"] else " · ARCHIVED")
        with st.expander(label):
            _class_detail(tutor_id, c)


def _class_detail(tutor_id: str, c: dict[str, Any]) -> None:
    cid = c["id"]
    st.markdown(f"**Class code:** `{c['join_code']}` — students enter it under *My Classes*.")
    mem = classes.members(tutor_id, cid)
    if mem:
        st.dataframe([{"Name": m["name"], "Student ID": m["student_id"], "Joined": classes.fmt_local(m["joined_at"])} for m in mem], hide_index=True, use_container_width=True)
        who = st.selectbox("Remove a student", [""] + [m["student_id"] for m in mem], format_func=lambda s: s or "—", key=f"cls_rm_{cid}")
        if who and st.button("Remove from class", key=f"cls_rmb_{cid}"):
            classes.remove_member(tutor_id, cid, who)
            st.rerun()
    else:
        st.caption("No students have joined yet.")

    if c["is_active"]:
        st.markdown("##### Assign a quiz")
        quizzes = [q for q in list_shared_quizzes(tutor_id)]
        if not quizzes:
            st.info("Publish a quiz first (Publish tab), then come back to assign it.")
        else:
            with st.form(f"cls_assign_{cid}"):
                pick = st.selectbox("Published quiz", quizzes, format_func=lambda q: f"{q['title']} · {q['code']}")
                title = st.text_input("Assignment title (optional)")
                has_due = st.checkbox("Set a deadline", value=True)
                d1, d2 = st.columns(2)
                due_d = d1.date_input("Due date", value=_local_now().date() + timedelta(days=3))
                due_t = d2.time_input("Due time", value=time(23, 59))
                late = st.checkbox("Allow late start (flagged as late)", value=False)
                if st.form_submit_button("Assign", type="primary"):
                    due = classes.local_to_utc_iso(due_d, due_t) if has_due else None
                    ok, msg = classes.create_assignment(tutor_id, cid, pick["code"], title, due, late)
                    (st.success if ok else st.error)(msg)
                    if ok:
                        st.rerun()

    reports = classes.list_assignments(tutor_id, cid)
    for r in reports:
        st.divider()
        due_txt = classes.fmt_local(r["due_at"])
        st.markdown(f"**{r['title']}** · quiz `{r['quiz_code']}` · due {due_txt}" + (" · late allowed" if r["allow_late"] else ""))
        avg = f" · average {r['average']}%" if r["average"] is not None else ""
        st.caption(f"{r['completed']}/{r['members']} completed · {r['late']} late{avg}")
        if r["rows"]:
            st.dataframe([{"Student": x["name"], "Status": STATUS_LABEL.get(x["status"], x["status"]), "Score %": x["score"], "Submitted": classes.fmt_local(x["submitted_at"]) if x["submitted_at"] else ""} for x in r["rows"]],
                         hide_index=True, use_container_width=True)
        if r["missing"]:
            st.warning("Not done yet: " + ", ".join(x["name"] for x in r["missing"]))
        b1, b2, b3 = st.columns(3)
        b1.download_button("Download CSV", report_csv(r), file_name=f"assignment_{r['quiz_code']}.csv", mime="text/csv", key=f"cls_csv_{r['id']}")
        with b2.popover("Change deadline"):
            cur = classes.utc_iso_to_local(r["due_at"])
            nd = st.date_input("Date", value=(cur.date() if cur else _local_now().date() + timedelta(days=1)), key=f"cls_nd_{r['id']}")
            nt = st.time_input("Time", value=(cur.time() if cur else time(23, 59)), key=f"cls_nt_{r['id']}")
            nl = st.checkbox("Allow late start", value=bool(r["allow_late"]), key=f"cls_nl_{r['id']}")
            if st.button("Save", key=f"cls_ns_{r['id']}"):
                classes.update_deadline(tutor_id, r["id"], classes.local_to_utc_iso(nd, nt), nl)
                st.rerun()
        if b3.button("Remove assignment", key=f"cls_del_{r['id']}"):
            classes.delete_assignment(tutor_id, r["id"])
            st.rerun()

    st.divider()
    if c["is_active"]:
        if st.button("Archive class", key=f"cls_arch_{cid}"):
            classes.set_class_active(tutor_id, cid, False)
            st.rerun()
    elif st.button("Restore class", key=f"cls_rest_{cid}"):
        classes.set_class_active(tutor_id, cid, True)
        st.rerun()


# ------------------------------------------------------------------ student
def render_my_classes(student_id: str, open_quiz: Callable[[str], None]) -> None:
    hero("🏫 My Classes", "Join your tutor's class with its code and see every assignment with its deadline.")
    with st.form("cls_join", clear_on_submit=True):
        code = st.text_input("Class code", placeholder="e.g. K7M2QX", max_chars=12)
        if st.form_submit_button("Join class", type="primary"):
            ok, msg, _ = classes.join_class(student_id, code)
            (st.success if ok else st.error)(msg)

    mine = classes.my_classes(student_id)
    if not mine:
        st.info("You have not joined a class yet.")
        return
    st.markdown("#### Assignments")
    items = classes.student_assignments(student_id)
    if not items:
        st.caption("No assignments yet.")
    for a in items:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{a['title']}** · {a['class_name']}")
            c1.caption(f"{STATUS_LABEL.get(a['status'], a['status'])} · due {classes.fmt_local(a['due_at'])}" + (f" · score {a['score']:.0f}%" if a["score"] is not None else ""))
            if a["status"] in ("open", "due_soon", "overdue") and a["is_open"]:
                if c2.button("Start", key=f"cls_go_{a['id']}", type="primary"):
                    open_quiz(a["quiz_code"])
    st.markdown("#### My classes")
    for c in mine:
        col1, col2 = st.columns([4, 1])
        col1.write(f"{c['name']} — {c['tutor_name']}" + ("" if c["is_active"] else " (closed)"))
        if col2.button("Leave", key=f"cls_leave_{c['id']}"):
            classes.leave_class(student_id, c["id"])
            st.rerun()
