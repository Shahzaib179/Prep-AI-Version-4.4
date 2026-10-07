from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from db_core import IntegrityError, connect, dialect  # noqa: F401  (re-exported: app code imports them from db)
from mastery_model import (
    MIN_ATTEMPTS_FOR_LABEL,
    STRONG_FROM,
    WEAK_BELOW,
    compute_mastery,
    mastery_label,
    norm,
)
from migrations import migrate

MASTERY_MODEL_VERSION = 2  # bump to force every student's mastery to be recomputed


def _today() -> str:
    return datetime.utcnow().date().isoformat()


def _meta_get(con: Any, key: str) -> str | None:
    row = con.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(con: Any, key: str, value: str) -> None:
    con.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def init_db() -> None:
    """Create / upgrade the schema (idempotent) and refresh derived mastery data when the model changed."""
    migrate()
    with connect() as con:
        stored = _meta_get(con, "mastery_model_version")
        if stored is None and con.dialect == "sqlite":
            # Databases from v4.3 and older tracked this in SQLite's PRAGMA user_version.
            stored = str(con.execute("PRAGMA user_version").fetchone()[0])
        needs_rebuild = int(stored or 0) < MASTERY_MODEL_VERSION
    if needs_rebuild:
        # Old scores were saved with the previous (inaccurate) formula: recompute them
        # from the raw answers, which were always stored correctly.
        with connect() as con:
            ids = [r[0] for r in con.execute("SELECT DISTINCT student_id FROM question_attempts").fetchall()]
        for sid in ids:
            rebuild_mastery(sid)
    with connect() as con:
        _meta_set(con, "mastery_model_version", str(MASTERY_MODEL_VERSION))


def ensure_student(student_id: str, name: str = "Student") -> None:
    now = datetime.utcnow().isoformat()
    with connect() as con:
        con.execute("INSERT INTO students(id,name,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING", (student_id, name, now, now))
        con.execute("UPDATE students SET name=?, updated_at=? WHERE id=?", (name, now, student_id))
        con.execute("INSERT INTO student_preferences(student_id) VALUES(?) ON CONFLICT(student_id) DO NOTHING", (student_id,))


def get_student(student_id: str) -> dict[str, Any]:
    with connect() as con:
        row = con.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    return dict(row) if row else {}


def update_student(student_id: str, **fields: Any) -> None:
    allowed = {"name", "exam_name", "exam_date", "level", "daily_minutes"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat()
    sql = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE students SET {sql} WHERE id=?", (*fields.values(), student_id))


def get_preferences(student_id: str) -> dict[str, Any]:
    with connect() as con:
        row = con.execute("SELECT * FROM student_preferences WHERE student_id=?", (student_id,)).fetchone()
    return dict(row) if row else {}


def update_preferences(student_id: str, **fields: str) -> None:
    allowed = {"preferred_difficulty", "preferred_language", "explanation_style", "learning_style", "llm_model", "ui_color", "theme_preset", "theme_bg", "theme_text"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    with connect() as con:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE student_preferences SET {sets} WHERE student_id=?", (*fields.values(), student_id))


def record_quiz(student_id: str, subject: str, topic: str, questions: list[dict[str, Any]], answers: dict[int, str], difficulty: str,
                *, shared_code: str | None = None, time_taken_sec: int | None = None, time_limit_sec: int = 0, timed_out: bool = False) -> int:
    correct = incorrect = skipped = 0
    now = datetime.utcnow().isoformat()
    with connect() as con:
        quiz_id = con.insert("INSERT INTO quiz_attempts(student_id,subject,topic,total,correct,incorrect,skipped,score,difficulty,created_at,shared_code,time_taken_sec,time_limit_sec,timed_out) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (student_id, subject, topic, len(questions), 0, 0, 0, 0, difficulty, now, shared_code, time_taken_sec, int(time_limit_sec or 0), int(bool(timed_out))))
        for i, q in enumerate(questions):
            selected = answers.get(i, "")
            correct_answer = str(q.get("answer", "")).strip()
            is_correct = bool(selected and selected == correct_answer)
            correct += int(is_correct)
            if not selected:
                skipped += 1
            else:
                incorrect += int(not is_correct)
            con.execute("INSERT INTO question_attempts(quiz_id,student_id,question_text,selected_answer,correct_answer,is_correct,subject,topic,concept,difficulty,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (quiz_id, student_id, q.get("question", ""), selected, correct_answer, int(is_correct), subject, topic, q.get("concept", ""), q.get("difficulty", difficulty), now))
            if not is_correct and selected:
                con.execute("INSERT INTO mistakes(student_id,subject,topic,concept,question_text,wrong_answer,correct_answer,explanation,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (student_id, subject, topic, q.get("concept", ""), q.get("question", ""), selected, correct_answer, q.get("explanation", ""), now))
        total = len(questions)
        score = (correct / total * 100) if total else 0
        con.execute("UPDATE quiz_attempts SET correct=?,incorrect=?,skipped=?,score=? WHERE id=?", (correct, incorrect, skipped, score, quiz_id))
    update_mastery_from_quiz(student_id, subject, topic, questions, answers)
    return int(quiz_id)


def _attempt_rows(student_id: str) -> list[Any]:
    """Every question the student was shown (answered or skipped), oldest first."""
    with connect() as con:
        return con.execute(
            "SELECT subject,topic,concept,difficulty,is_correct,created_at,"
            "(TRIM(COALESCE(selected_answer,''))='') AS skipped FROM question_attempts "
            "WHERE student_id=? ORDER BY id ASC",
            (student_id,),
        ).fetchall()


def _group(rows: list[Any], level: str) -> dict[tuple, dict[str, Any]]:
    """Group attempts by (subject, topic) or (subject, topic, concept), ignoring case/spaces."""
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        topic = (r["topic"] or "").strip() or "General"
        concept = (r["concept"] or "").strip() or topic
        key = (norm(r["subject"]), norm(topic)) + ((norm(concept),) if level == "concept" else ())
        g = groups.setdefault(key, {"attempts": []})
        g["attempts"].append({"is_correct": bool(r["is_correct"]), "difficulty": r["difficulty"], "skipped": bool(r["skipped"])})
        # keep the FIRST spelling for display so 'Biology' and 'biology ' show as one stable name
        g.setdefault("subject", (r["subject"] or "").strip() or "General")
        g.setdefault("topic", topic)
        g.setdefault("concept", concept)
        g["last_studied"] = r["created_at"]
    return groups


def rebuild_mastery(student_id: str) -> None:
    """Recompute concept mastery + revision schedule from the raw answers (single source of truth)."""
    rows = _attempt_rows(student_id)
    concept_groups = _group(rows, "concept")
    topic_groups = _group(rows, "topic")
    with connect() as con:
        con.execute("DELETE FROM mastery WHERE student_id=?", (student_id,))
        for key, g in concept_groups.items():
            m = compute_mastery(g["attempts"])
            parent = topic_groups[key[:2]]          # use the topic's display spelling everywhere
            g["subject"], g["topic"] = parent["subject"], parent["topic"]
            con.execute(
                "INSERT INTO mastery(student_id,subject,chapter,topic,concept,attempts,correct,accuracy,"
                "difficulty_score,recent_accuracy,repeated_mistakes,mastery_score,last_studied) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(student_id,subject,chapter,topic,concept) DO UPDATE SET attempts=excluded.attempts,"
                "correct=excluded.correct,accuracy=excluded.accuracy,difficulty_score=excluded.difficulty_score,"
                "recent_accuracy=excluded.recent_accuracy,repeated_mistakes=excluded.repeated_mistakes,"
                "mastery_score=excluded.mastery_score,last_studied=excluded.last_studied",
                (student_id, g["subject"], "", g["topic"], g["concept"], m["attempts"], m["correct"],
                 m["accuracy"], m["difficulty_score"], m["recent_accuracy"], m["wrong"], m["score"],
                 g["last_studied"]),
            )
        con.execute("DELETE FROM revision_schedule WHERE student_id=?", (student_id,))
    for g in topic_groups.values():
        schedule_revision(student_id, g["subject"], g["topic"], compute_mastery(g["attempts"])["score"],
                          studied_on=str(g["last_studied"])[:10])


def update_mastery_from_quiz(student_id: str, subject: str, topic: str, questions: list[dict[str, Any]], answers: dict[int, str]) -> None:
    """Called after every quiz. Answers are already saved, so just recompute from them."""
    rebuild_mastery(student_id)


def schedule_revision(student_id: str, subject: str, topic: str, mastery_score: float, studied_on: str | None = None) -> None:
    if mastery_score < 40:
        interval = 1
    elif mastery_score < 60:
        interval = 2
    elif mastery_score < 75:
        interval = 4
    elif mastery_score < 90:
        interval = 7
    else:
        interval = 14
    try:
        base = date.fromisoformat(studied_on[:10]) if studied_on else datetime.utcnow().date()
    except ValueError:
        base = datetime.utcnow().date()
    next_review = (base + timedelta(days=interval)).isoformat()
    with connect() as con:
        con.execute("INSERT INTO revision_schedule(student_id,subject,topic,next_review,interval_days,mastery,last_studied) VALUES(?,?,?,?,?,?,?) ON CONFLICT(student_id,subject,topic) DO UPDATE SET next_review=excluded.next_review,interval_days=excluded.interval_days,mastery=excluded.mastery,last_studied=excluded.last_studied", (student_id, subject, topic, next_review, interval, mastery_score, studied_on or datetime.utcnow().isoformat()))


def topic_mastery(student_id: str) -> list[dict[str, Any]]:
    """One row per (subject, topic): mastery pooled over ALL answers in that topic."""
    out = []
    for g in _group(_attempt_rows(student_id), "topic").values():
        m = compute_mastery(g["attempts"])
        out.append({
            "subject": g["subject"], "topic": g["topic"], "mastery_score": m["score"],
            "attempts": m["attempts"], "accuracy": m["accuracy"], "recent_accuracy": m["recent_accuracy"],
            "wrong": m["wrong"], "skipped": m["skipped"], "label": mastery_label(m["score"]),
            "enough_data": m["attempts"] >= MIN_ATTEMPTS_FOR_LABEL,
        })
    return out


def weak_strong_areas(student_id: str, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    """Dashboard buckets. Topics with too few answers go to 'building' instead of being mislabeled."""
    rows = topic_mastery(student_id)
    solid = [r for r in rows if r["enough_data"]]
    return {
        "weak": sorted((r for r in solid if r["mastery_score"] < WEAK_BELOW), key=lambda r: (r["mastery_score"], -r["attempts"]))[:limit],
        "strong": sorted((r for r in solid if r["mastery_score"] >= STRONG_FROM), key=lambda r: (-r["mastery_score"], -r["attempts"]))[:limit],
        "developing": [r for r in solid if WEAK_BELOW <= r["mastery_score"] < STRONG_FROM],
        "building": [r for r in rows if not r["enough_data"]],
    }


def get_topic_mastery(student_id: str, subject: str, topic: str) -> dict[str, Any] | None:
    for r in topic_mastery(student_id):
        if norm(r["subject"]) == norm(subject) and norm(r["topic"]) == norm(topic):
            return r
    return None


def dashboard_stats(student_id: str) -> dict[str, Any]:
    rows = _attempt_rows(student_id)
    topics = topic_mastery(student_id)
    answered = [r for r in rows if not r["skipped"]]
    correct = sum(1 for r in answered if r["is_correct"])
    with connect() as con:
        due = con.execute("SELECT COUNT(*) FROM revision_schedule WHERE student_id=? AND next_review<=?", (student_id, _today())).fetchone()[0]
    overall = sum(t["mastery_score"] for t in topics) / len(topics) if topics else 0.0
    ranked = sorted(topics, key=lambda t: t["mastery_score"])
    return {
        "overall": round(overall, 1), "attempted": len(answered), "skipped": len(rows) - len(answered),
        "correct": correct, "incorrect": len(answered) - correct,
        "accuracy": round(100 * correct / max(1, len(answered)), 1),
        "weak": ranked[0] if ranked else None, "strong": ranked[-1] if ranked else None,
        "topics_tracked": len(topics), "revision_due": due,
    }


def weak_topics(student_id: str, limit: int = 10, max_score: float | None = STRONG_FROM) -> list[dict[str, Any]]:
    """Concept rows that still need practice (below 75%), weakest first."""
    sql = ("SELECT subject,topic,concept,mastery_score,attempts,repeated_mistakes FROM mastery WHERE student_id=?")
    params: list[Any] = [student_id]
    if max_score is not None:
        sql += " AND mastery_score < ?"
        params.append(max_score)
    sql += " ORDER BY mastery_score ASC, attempts DESC LIMIT ?"
    params.append(limit)
    with connect() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def recent_mistakes(student_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM mistakes WHERE student_id=? ORDER BY id DESC LIMIT ?", (student_id, limit)).fetchall()
    return [dict(r) for r in rows]


def due_revisions(student_id: str) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM revision_schedule WHERE student_id=? AND next_review<=? ORDER BY mastery ASC", (student_id, _today())).fetchall()
    return [dict(r) for r in rows]


def revision_recommendations(student_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return weak topics that are worth revising even before their scheduled date."""
    with connect() as con:
        rows = con.execute(
            """
            SELECT
                m.subject,
                m.topic,
                m.concept,
                m.mastery_score,
                m.attempts,
                m.repeated_mistakes,
                m.last_studied,
                r.next_review,
                r.interval_days
            FROM mastery AS m
            LEFT JOIN revision_schedule AS r
              ON r.student_id = m.student_id
             AND r.subject = m.subject
             AND r.topic = m.topic
            WHERE m.student_id = ?
              AND m.mastery_score < 75
            ORDER BY m.mastery_score ASC,
                     m.repeated_mistakes DESC,
                     m.attempts DESC
            LIMIT ?
            """,
            (student_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def upcoming_revisions(student_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return scheduled revisions whose review date is still in the future."""
    with connect() as con:
        rows = con.execute(
            """
            SELECT *
            FROM revision_schedule
            WHERE student_id = ?
              AND next_review > ?
            ORDER BY next_review ASC, mastery ASC
            LIMIT ?
            """,
            (student_id, _today(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def history(student_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM quiz_attempts WHERE student_id=? ORDER BY id DESC LIMIT ?", (student_id, limit)).fetchall()
    return [dict(r) for r in rows]


def save_plan(student_id: str, exam_name: str, exam_date: str, plan: Any) -> None:
    with connect() as con:
        con.execute("INSERT INTO study_plans(student_id,exam_name,exam_date,plan_json,created_at) VALUES(?,?,?,?,?)", (student_id, exam_name, exam_date, json.dumps(plan), datetime.utcnow().isoformat()))


def add_achievement(student_id: str, code: str, title: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO achievements(student_id,code,title,unlocked_at) VALUES(?,?,?,?) ON CONFLICT(student_id,code) DO NOTHING", (student_id, code, title, datetime.utcnow().isoformat()))


def achievements(student_id: str) -> list[dict[str, Any]]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM achievements WHERE student_id=? ORDER BY id DESC", (student_id,)).fetchall()]


def save_agent_session(student_id: str, agent_name: str, user_input: str, output: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO agent_sessions(student_id,agent_name,user_input,output,created_at) VALUES(?,?,?,?,?)", (student_id, agent_name, user_input, output, datetime.utcnow().isoformat()))


def get_agent_sessions(student_id: str, agent_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as con:
        if agent_name:
            rows = con.execute(
                "SELECT * FROM agent_sessions WHERE student_id=? AND agent_name=? ORDER BY id DESC LIMIT ?",
                (student_id, agent_name, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM agent_sessions WHERE student_id=? ORDER BY id DESC LIMIT ?",
                (student_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------------
# Merit Aggregate Agent history (used to pre-fill Path Finder)
# -----------------------------------------------------------------------------
def save_merit_result(student_id: str, exam: str, formula_id: str, formula_name: str, program: str, marks: dict[str, Any], aggregate: float) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO merit_results(student_id,exam,formula_id,formula_name,program,marks_json,aggregate,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (student_id, exam, formula_id, formula_name, program or "", json.dumps(marks), float(aggregate), datetime.utcnow().isoformat()),
        )


def merit_results(student_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM merit_results WHERE student_id=? ORDER BY id DESC LIMIT ?", (student_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["marks"] = json.loads(d.pop("marks_json") or "{}")
        except json.JSONDecodeError:
            d["marks"] = {}
        out.append(d)
    return out


# -----------------------------------------------------------------------------
# Roles (student / tutor) and activity
# -----------------------------------------------------------------------------
def set_role(student_id: str, role: str) -> None:
    if role not in ("student", "tutor"):
        raise ValueError("role must be 'student' or 'tutor'")
    with connect() as con:
        con.execute("UPDATE students SET role=? WHERE id=?", (role, student_id))


def get_role(student_id: str) -> str:
    with connect() as con:
        row = con.execute("SELECT role FROM students WHERE id=?", (student_id,)).fetchone()
    return (row["role"] if row and row["role"] else "student")


def touch_active(student_id: str) -> None:
    with connect() as con:
        con.execute("UPDATE students SET last_active=? WHERE id=?", (datetime.utcnow().isoformat(), student_id))


def delete_agent_sessions(student_id: str) -> int:
    """Remove saved tutor/research conversation history for one student."""
    with connect() as con:
        return con.execute("DELETE FROM agent_sessions WHERE student_id=?", (student_id,)).rowcount


# -----------------------------------------------------------------------------
# Shared quizzes: one question set, many students, each attempt stored separately
# -----------------------------------------------------------------------------
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L lookalikes


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def create_shared_quiz(created_by: str, title: str, subject: str, topic: str, difficulty: str,
                       questions: list[dict[str, Any]], time_limit_sec: int = 0, code_length: int = 6) -> str:
    import secrets
    now = datetime.utcnow().isoformat()
    for _ in range(20):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(code_length))
        try:
            with connect() as con:
                con.execute(
                    "INSERT INTO shared_quizzes(code,title,subject,topic,difficulty,questions_json,time_limit_sec,created_by,created_at,is_open) VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (code, title, subject, topic, difficulty, json.dumps(questions), int(time_limit_sec or 0), created_by, now),
                )
            return code
        except IntegrityError:
            continue
    raise RuntimeError("Could not generate a unique quiz code.")


def get_shared_quiz(code: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM shared_quizzes WHERE code=?", (normalize_code(code),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["questions"] = json.loads(d.pop("questions_json") or "[]")
    return d


def set_shared_quiz_open(code: str, is_open: bool, owner_id: str | None = None) -> bool:
    """Open/close a shared quiz. When ``owner_id`` is given, only that creator may change it."""
    sql, params = "UPDATE shared_quizzes SET is_open=? WHERE code=?", [int(is_open), normalize_code(code)]
    if owner_id is not None:
        sql += " AND created_by=?"
        params.append(owner_id)
    with connect() as con:
        return con.execute(sql, params).rowcount > 0


def list_shared_quizzes(created_by: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT s.code,s.title,s.subject,s.topic,s.difficulty,s.time_limit_sec,s.created_by,s.created_at,s.is_open,"
           "(SELECT COUNT(*) FROM quiz_attempts a WHERE a.shared_code=s.code) AS attempts,"
           "(SELECT AVG(a.score) FROM quiz_attempts a WHERE a.shared_code=s.code) AS avg_score,"
           "(SELECT name FROM students WHERE id=s.created_by) AS creator_name FROM shared_quizzes s")
    params: tuple = ()
    if created_by:
        sql += " WHERE s.created_by=?"
        params = (created_by,)
    sql += " ORDER BY s.created_at DESC"
    with connect() as con:
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    for r in rows:
        r["avg_score"] = None if r.get("avg_score") is None else round(float(r["avg_score"]), 1)
    return rows


def shared_quiz_start(code: str, student_id: str) -> str:
    """Record (once) when a student opened a shared quiz; return the ORIGINAL start time.

    Stored server-side so reloading the browser cannot restart a timed quiz.
    """
    code = normalize_code(code)
    with connect() as con:
        con.execute("INSERT INTO shared_quiz_starts(code,student_id,started_at) VALUES(?,?,?) ON CONFLICT(code,student_id) DO NOTHING", (code, student_id, datetime.utcnow().isoformat()))
        row = con.execute("SELECT started_at FROM shared_quiz_starts WHERE code=? AND student_id=?", (code, student_id)).fetchone()
    return row["started_at"]


def has_attempted_shared(code: str, student_id: str) -> bool:
    with connect() as con:
        return con.execute("SELECT 1 FROM quiz_attempts WHERE shared_code=? AND student_id=?", (normalize_code(code), student_id)).fetchone() is not None


def record_shared_timeout(student_id: str, quiz: dict[str, Any], time_taken_sec: int) -> int:
    """The student's time ran out before they came back: store a 0-answered attempt.

    Deliberately NOT sent through mastery (no question_attempts rows) so a lost
    connection does not damage the student's topic scores.
    """
    now = datetime.utcnow().isoformat()
    total = len(quiz["questions"])
    with connect() as con:
        return con.insert(
            "INSERT INTO quiz_attempts(student_id,subject,topic,total,correct,incorrect,skipped,score,difficulty,created_at,shared_code,time_taken_sec,time_limit_sec,timed_out) VALUES(?,?,?,?,0,0,?,0,?,?,?,?,?,1)",
            (student_id, quiz["subject"], quiz["topic"], total, total, quiz["difficulty"], now, quiz["code"], int(time_taken_sec), int(quiz.get("time_limit_sec") or 0)),
        )


def shared_quiz_results(code: str) -> list[dict[str, Any]]:
    """Every student's attempt on one shared quiz, best first (ties: faster first)."""
    with connect() as con:
        rows = con.execute(
            "SELECT a.student_id, COALESCE(s.name,a.student_id) AS name, a.score, a.correct, a.incorrect, a.skipped, a.total, "
            "a.time_taken_sec, a.timed_out, a.created_at FROM quiz_attempts a LEFT JOIN students s ON s.id=a.student_id "
            "WHERE a.shared_code=? ORDER BY a.score DESC, COALESCE(a.time_taken_sec, 999999) ASC",
            (normalize_code(code),),
        ).fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------------
# Tutor view: performance of every individual student
# -----------------------------------------------------------------------------
def roster(tutor_id: str | None = None) -> list[dict[str, Any]]:
    """One summary row per student (tutors are excluded).

    With ``tutor_id`` only the students who took at least one quiz published by that tutor are listed,
    so one tutor never sees another tutor's class.
    """
    sql = "SELECT id,name,level,created_at,last_active FROM students WHERE COALESCE(role,'student')='student'"
    params: tuple = ()
    if tutor_id is not None:
        sql += (" AND id IN (SELECT a.student_id FROM quiz_attempts a JOIN shared_quizzes q ON q.code=a.shared_code WHERE q.created_by=?)")
        params = (tutor_id,)
    with connect() as con:
        studs = [dict(r) for r in con.execute(sql + " ORDER BY LOWER(name)", params).fetchall()]
        quiz = {r["student_id"]: dict(r) for r in con.execute(
            "SELECT student_id, COUNT(*) AS quizzes, AVG(score) AS avg_score, MAX(created_at) AS last_quiz, SUM(timed_out) AS timeouts FROM quiz_attempts GROUP BY student_id").fetchall()}
    out = []
    for s in studs:
        q = quiz.get(s["id"], {})
        st = dashboard_stats(s["id"])
        out.append({
            "student_id": s["id"], "name": s["name"], "level": s["level"],
            "quizzes": q.get("quizzes", 0), "avg_score": round(float(q.get("avg_score") or 0.0), 1),
            "questions_answered": st["attempted"], "accuracy": st["accuracy"], "mastery": st["overall"],
            "revision_due": st["revision_due"], "timeouts": int(q.get("timeouts") or 0),
            "last_active": q.get("last_quiz") or s.get("last_active") or s["created_at"],
        })
    return out


def student_quiz_trend(student_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Quiz attempts oldest -> newest (for line charts)."""
    with connect() as con:
        rows = con.execute(
            "SELECT id,subject,topic,total,correct,incorrect,skipped,score,difficulty,created_at,shared_code,time_taken_sec,time_limit_sec,timed_out "
            "FROM quiz_attempts WHERE student_id=? ORDER BY id DESC LIMIT ?", (student_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def subject_breakdown(student_id: str) -> list[dict[str, Any]]:
    """Accuracy per subject across every answered question."""
    groups: dict[str, list[int]] = {}
    for r in _attempt_rows(student_id):
        if r["skipped"]:
            continue
        groups.setdefault((r["subject"] or "General").strip() or "General", []).append(int(r["is_correct"]))
    return [{"subject": k, "answered": len(v), "accuracy": round(100 * sum(v) / len(v), 1)} for k, v in sorted(groups.items())]
