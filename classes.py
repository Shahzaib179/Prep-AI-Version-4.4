"""Tutor classes, student enrolment, and assignments with deadlines.

* A tutor creates a class; students join with a 6-character class code.
* A tutor assigns one of THEIR published quizzes to a class with a due date (stored in UTC).
* Deadline rule: a student must START before the due time. Starting later is refused unless the assignment allows late work
  (late attempts are then flagged). A quiz started in time may be finished after the due time.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta
from typing import Any

import config
from db_core import cached_read, connect, IntegrityError

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
DUE_SOON_HOURS = 48


# ------------------------------------------------------------------ time helpers
def now_utc() -> datetime:
    return datetime.utcnow()


def local_to_utc_iso(d: date, t: time) -> str:
    """Date + time typed in the app's local timezone -> UTC ISO string."""
    return (datetime.combine(d, t) - timedelta(minutes=config.PROGRESS_TZ_OFFSET_MIN)).isoformat(timespec="minutes")


def utc_iso_to_local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso) + timedelta(minutes=config.PROGRESS_TZ_OFFSET_MIN)


def fmt_local(iso: str | None) -> str:
    dt = utc_iso_to_local(iso)
    return dt.strftime("%a %d %b %Y, %I:%M %p") if dt else "No deadline"


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


# ------------------------------------------------------------------ classes
def create_class(tutor_id: str, name: str) -> dict[str, Any]:
    name = " ".join((name or "").split())
    if not (2 <= len(name) <= 80):
        raise ValueError("Class name must be 2-80 characters.")
    now = now_utc().isoformat()
    for _ in range(20):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        try:
            with connect() as con:
                cid = con.insert("INSERT INTO classes(tutor_id,name,join_code,created_at,is_active) VALUES(?,?,?,?,1)", (tutor_id, name, code, now))
            return {"id": cid, "name": name, "join_code": code}
        except IntegrityError:
            continue
    raise RuntimeError("Could not generate a unique class code.")


def get_class(class_id: int) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT id,tutor_id,name,join_code,created_at,is_active FROM classes WHERE id=?", (class_id,)).fetchone()
    return dict(r) if r else None


@cached_read(30)
def list_classes(tutor_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = ("SELECT c.id,c.name,c.join_code,c.created_at,c.is_active,"
           "(SELECT COUNT(*) FROM class_members m WHERE m.class_id=c.id) AS members,"
           "(SELECT COUNT(*) FROM assignments a WHERE a.class_id=c.id) AS assignments FROM classes c WHERE c.tutor_id=?")
    if not include_archived:
        sql += " AND c.is_active=1"
    with connect() as con:
        return [dict(r) for r in con.execute(sql + " ORDER BY c.id DESC", (tutor_id,)).fetchall()]


def set_class_active(tutor_id: str, class_id: int, active: bool) -> bool:
    with connect() as con:
        return con.execute("UPDATE classes SET is_active=? WHERE id=? AND tutor_id=?", (int(active), class_id, tutor_id)).rowcount > 0


def join_class(student_id: str, code: str) -> tuple[bool, str, dict[str, Any] | None]:
    code = normalize_code(code)
    with connect() as con:
        c = con.execute("SELECT id,tutor_id,name,is_active FROM classes WHERE join_code=?", (code,)).fetchone()
        if not c:
            return False, "No class found with that code.", None
        c = dict(c)
        if not c["is_active"]:
            return False, "This class is closed.", None
        if c["tutor_id"] == student_id:
            return False, "You cannot join your own class.", None
        role = con.execute("SELECT role FROM students WHERE id=?", (student_id,)).fetchone()
        if role and (role["role"] or "student") != "student":
            return False, "Only students can join a class.", None
        if con.execute("SELECT 1 FROM class_members WHERE class_id=? AND student_id=?", (c["id"], student_id)).fetchone():
            return True, f"You are already in {c['name']}.", c
        con.execute("INSERT INTO class_members(class_id,student_id,joined_at) VALUES(?,?,?) ON CONFLICT(class_id,student_id) DO NOTHING", (c["id"], student_id, now_utc().isoformat()))
    return True, f"Joined {c['name']}.", c


def leave_class(student_id: str, class_id: int) -> bool:
    with connect() as con:
        return con.execute("DELETE FROM class_members WHERE class_id=? AND student_id=?", (class_id, student_id)).rowcount > 0


def remove_member(tutor_id: str, class_id: int, student_id: str) -> bool:
    with connect() as con:
        if not con.execute("SELECT 1 FROM classes WHERE id=? AND tutor_id=?", (class_id, tutor_id)).fetchone():
            return False
        return con.execute("DELETE FROM class_members WHERE class_id=? AND student_id=?", (class_id, student_id)).rowcount > 0


@cached_read(30)
def members(tutor_id: str, class_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT m.student_id, COALESCE(s.name, m.student_id) AS name, m.joined_at FROM class_members m JOIN classes c ON c.id=m.class_id "
                           "LEFT JOIN students s ON s.id=m.student_id WHERE m.class_id=? AND c.tutor_id=? ORDER BY LOWER(COALESCE(s.name, m.student_id))", (class_id, tutor_id)).fetchall()
    return [dict(r) for r in rows]


@cached_read(30)
def my_classes(student_id: str) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT c.id,c.name,c.is_active,COALESCE(t.name,'Tutor') AS tutor_name FROM class_members m JOIN classes c ON c.id=m.class_id "
                           "LEFT JOIN students t ON t.id=c.tutor_id WHERE m.student_id=? ORDER BY c.name", (student_id,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ assignments
def create_assignment(tutor_id: str, class_id: int, quiz_code: str, title: str, due_at_utc: str | None, allow_late: bool = False) -> tuple[bool, str]:
    quiz_code = normalize_code(quiz_code)
    with connect() as con:
        if not con.execute("SELECT 1 FROM classes WHERE id=? AND tutor_id=? AND is_active=1", (class_id, tutor_id)).fetchone():
            return False, "Class not found or closed."
        q = con.execute("SELECT title FROM shared_quizzes WHERE code=? AND created_by=?", (quiz_code, tutor_id)).fetchone()
        if not q:
            return False, "You can only assign quizzes you published yourself."
        if due_at_utc and datetime.fromisoformat(due_at_utc) <= now_utc():
            return False, "The deadline must be in the future."
        if con.execute("SELECT 1 FROM assignments WHERE class_id=? AND quiz_code=?", (class_id, quiz_code)).fetchone():
            return False, "This quiz is already assigned to this class."
        con.execute("INSERT INTO assignments(class_id,tutor_id,quiz_code,title,due_at,allow_late,created_at) VALUES(?,?,?,?,?,?,?)",
                    (class_id, tutor_id, quiz_code, (title or q["title"] or quiz_code).strip(), due_at_utc, int(allow_late), now_utc().isoformat()))
        con.execute("UPDATE shared_quizzes SET is_open=1 WHERE code=?", (quiz_code,))
    return True, "Assignment created."


def delete_assignment(tutor_id: str, assignment_id: int) -> bool:
    with connect() as con:
        return con.execute("DELETE FROM assignments WHERE id=? AND tutor_id=?", (assignment_id, tutor_id)).rowcount > 0


def update_deadline(tutor_id: str, assignment_id: int, due_at_utc: str | None, allow_late: bool) -> bool:
    with connect() as con:
        return con.execute("UPDATE assignments SET due_at=?, allow_late=? WHERE id=? AND tutor_id=?", (due_at_utc, int(allow_late), assignment_id, tutor_id)).rowcount > 0


def _attempt_info(con: Any, code: str, student_id: str) -> dict[str, Any] | None:
    r = con.execute("SELECT a.score,a.correct,a.total,a.created_at,a.time_taken_sec,a.timed_out,st.started_at FROM quiz_attempts a "
                    "LEFT JOIN shared_quiz_starts st ON st.code=a.shared_code AND st.student_id=a.student_id WHERE a.shared_code=? AND a.student_id=?", (code, student_id)).fetchone()
    return dict(r) if r else None


def status_for(due_at: str | None, allow_late: bool, attempt: dict[str, Any] | None, now: datetime | None = None) -> str:
    """done / done_late / open / due_soon / overdue (can still start: late allowed) / missed (cannot start any more)."""
    now = now or now_utc()
    due = datetime.fromisoformat(due_at) if due_at else None
    if attempt:
        began = datetime.fromisoformat(attempt.get("started_at") or attempt["created_at"])
        return "done_late" if due and began > due else "done"
    if not due:
        return "open"
    if now > due:
        return "overdue" if allow_late else "missed"
    return "due_soon" if due - now <= timedelta(hours=DUE_SOON_HOURS) else "open"


@cached_read(30)
def student_assignments(student_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT a.id,a.class_id,a.quiz_code,a.title,a.due_at,a.allow_late,c.name AS class_name,q.is_open,q.time_limit_sec FROM assignments a "
                           "JOIN class_members m ON m.class_id=a.class_id JOIN classes c ON c.id=a.class_id JOIN shared_quizzes q ON q.code=a.quiz_code "
                           "WHERE m.student_id=?", (student_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            att = _attempt_info(con, d["quiz_code"], student_id)
            d["status"] = status_for(d["due_at"], bool(d["allow_late"]), att, now)
            d["score"] = att["score"] if att else None
            out.append(d)
    order = {"overdue": 0, "due_soon": 1, "open": 2, "missed": 3, "done_late": 4, "done": 5}
    out.sort(key=lambda d: (order[d["status"]], d["due_at"] or "9999"))
    return out


def deadline_state(student_id: str, quiz_code: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Is this quiz an assignment for the student? If so, may they still start it? None = not assigned to them (no deadline)."""
    quiz_code = normalize_code(quiz_code)
    now = now or now_utc()
    with connect() as con:
        rows = con.execute("SELECT a.due_at,a.allow_late FROM assignments a JOIN class_members m ON m.class_id=a.class_id WHERE a.quiz_code=? AND m.student_id=?", (quiz_code, student_id)).fetchall()
    if not rows:
        return None
    best = None   # the most generous binding wins when a quiz is assigned through several classes
    for r in rows:
        due = datetime.fromisoformat(r["due_at"]) if r["due_at"] else None
        ok = due is None or now <= due or bool(r["allow_late"])
        cand = {"due_at": r["due_at"], "allow_late": bool(r["allow_late"]), "blocked": not ok, "late": bool(due and now > due)}
        if best is None or (best["blocked"] and not cand["blocked"]) or (best["blocked"] == cand["blocked"] and (cand["due_at"] or "9999") > (best["due_at"] or "9999")):
            best = cand
    return best


@cached_read(30)
def assignment_report(tutor_id: str, assignment_id: int) -> dict[str, Any] | None:
    with connect() as con:
        a = con.execute("SELECT a.id,a.class_id,a.quiz_code,a.title,a.due_at,a.allow_late,c.name AS class_name FROM assignments a JOIN classes c ON c.id=a.class_id WHERE a.id=? AND a.tutor_id=?", (assignment_id, tutor_id)).fetchone()
        if not a:
            return None
        a = dict(a)
        mem = con.execute("SELECT m.student_id, COALESCE(s.name,m.student_id) AS name FROM class_members m LEFT JOIN students s ON s.id=m.student_id WHERE m.class_id=? ORDER BY LOWER(COALESCE(s.name,m.student_id))", (a["class_id"],)).fetchall()
        rows = []
        for m in mem:
            att = _attempt_info(con, a["quiz_code"], m["student_id"])
            rows.append({"student_id": m["student_id"], "name": m["name"], "status": status_for(a["due_at"], bool(a["allow_late"]), att),
                         "score": round(float(att["score"]), 1) if att else None, "submitted_at": att["created_at"] if att else None,
                         "time_taken_sec": att["time_taken_sec"] if att else None})
    done = [r for r in rows if r["status"] in ("done", "done_late")]
    return {**a, "rows": rows, "members": len(rows), "completed": len(done), "late": sum(r["status"] == "done_late" for r in rows),
            "missing": [r for r in rows if r["status"] not in ("done", "done_late")],
            "average": round(sum(r["score"] for r in done) / len(done), 1) if done else None}


@cached_read(30)
def list_assignments(tutor_id: str, class_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM assignments WHERE class_id=? AND tutor_id=? ORDER BY id DESC", (class_id, tutor_id)).fetchall()]
    return [assignment_report(tutor_id, i) for i in ids if i]


@cached_read(30)
def pending_count(student_id: str) -> int:
    """Assignments the student still has to do (for the sidebar badge)."""
    return sum(1 for a in student_assignments(student_id) if a["status"] in ("overdue", "due_soon", "open"))
