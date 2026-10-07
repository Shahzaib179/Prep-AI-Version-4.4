"""Versioned schema migrations (SQLite + Postgres).

Each migration runs once, in order, and is recorded in ``schema_migrations``.
Never edit a migration that has shipped: add a new one at the end of MIGRATIONS.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from db_core import Connection, connect

_PG_LOCK_KEY = 727274  # arbitrary app-wide advisory lock id (stops two app instances migrating at once)


def _types(con: Connection) -> dict[str, str]:
    if con.dialect == "postgres":
        return {"PK": "BIGSERIAL PRIMARY KEY", "REAL": "DOUBLE PRECISION", "BLOB": "BYTEA"}
    return {"PK": "INTEGER PRIMARY KEY AUTOINCREMENT", "REAL": "REAL", "BLOB": "BLOB"}


def _m001_baseline(con: Connection) -> None:
    t = _types(con)
    con.script(f"""
    CREATE TABLE IF NOT EXISTS students (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, exam_name TEXT DEFAULT '',
        exam_date TEXT, level TEXT DEFAULT 'MDCAT', daily_minutes INTEGER DEFAULT 60,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        role TEXT DEFAULT 'student', last_active TEXT
    );
    CREATE TABLE IF NOT EXISTS student_preferences (
        student_id TEXT PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
        preferred_difficulty TEXT DEFAULT 'Medium', preferred_language TEXT DEFAULT 'English',
        explanation_style TEXT DEFAULT 'Detailed', learning_style TEXT DEFAULT 'Examples + Practice',
        llm_model TEXT DEFAULT 'openai/gpt-oss-120b', ui_color TEXT DEFAULT 'Blue',
        theme_preset TEXT DEFAULT 'Default', theme_bg TEXT DEFAULT '', theme_text TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS study_sessions (
        id {t['PK']}, student_id TEXT REFERENCES students(id),
        subject TEXT, chapter TEXT, topic TEXT, mode TEXT, started_at TEXT, minutes INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS questions (
        id {t['PK']}, student_id TEXT, question_text TEXT, subject TEXT,
        topic TEXT, concept TEXT, difficulty TEXT, correct_answer TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS quiz_attempts (
        id {t['PK']}, student_id TEXT, subject TEXT, topic TEXT,
        total INTEGER, correct INTEGER, incorrect INTEGER, skipped INTEGER, score {t['REAL']},
        difficulty TEXT, created_at TEXT,
        shared_code TEXT, time_taken_sec INTEGER, time_limit_sec INTEGER DEFAULT 0, timed_out INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS question_attempts (
        id {t['PK']}, quiz_id INTEGER, student_id TEXT,
        question_text TEXT, selected_answer TEXT, correct_answer TEXT, is_correct INTEGER,
        subject TEXT, topic TEXT, concept TEXT, difficulty TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS mistakes (
        id {t['PK']}, student_id TEXT, subject TEXT, topic TEXT,
        concept TEXT, question_text TEXT, wrong_answer TEXT, correct_answer TEXT,
        explanation TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS mastery (
        student_id TEXT, subject TEXT, chapter TEXT DEFAULT '', topic TEXT DEFAULT '', concept TEXT DEFAULT '',
        attempts INTEGER DEFAULT 0, correct INTEGER DEFAULT 0, accuracy {t['REAL']} DEFAULT 0,
        difficulty_score {t['REAL']} DEFAULT 0, recent_accuracy {t['REAL']} DEFAULT 0, repeated_mistakes INTEGER DEFAULT 0,
        mastery_score {t['REAL']} DEFAULT 0, last_studied TEXT, PRIMARY KEY(student_id, subject, chapter, topic, concept)
    );
    CREATE TABLE IF NOT EXISTS revision_schedule (
        student_id TEXT, subject TEXT, topic TEXT, next_review TEXT, interval_days INTEGER DEFAULT 1,
        mastery {t['REAL']} DEFAULT 0, last_studied TEXT, PRIMARY KEY(student_id, subject, topic)
    );
    CREATE TABLE IF NOT EXISTS study_plans (
        id {t['PK']}, student_id TEXT, exam_name TEXT, exam_date TEXT,
        plan_json TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS achievements (
        id {t['PK']}, student_id TEXT, code TEXT, title TEXT,
        unlocked_at TEXT, UNIQUE(student_id, code)
    );
    CREATE TABLE IF NOT EXISTS memories (
        id {t['PK']}, student_id TEXT, memory_type TEXT, content TEXT,
        subject TEXT DEFAULT '', topic TEXT DEFAULT '', importance {t['REAL']} DEFAULT 0.5,
        confidence {t['REAL']} DEFAULT 0.7, created_at TEXT, updated_at TEXT, last_used_at TEXT, is_active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS agent_sessions (
        id {t['PK']}, student_id TEXT, agent_name TEXT, user_input TEXT,
        output TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS merit_results (
        id {t['PK']}, student_id TEXT, exam TEXT, formula_id TEXT,
        formula_name TEXT, program TEXT, marks_json TEXT, aggregate {t['REAL']}, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS shared_quizzes (
        code TEXT PRIMARY KEY, title TEXT, subject TEXT, topic TEXT, difficulty TEXT,
        questions_json TEXT NOT NULL, time_limit_sec INTEGER DEFAULT 0,
        created_by TEXT, created_at TEXT, is_open INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS shared_quiz_starts (
        code TEXT, student_id TEXT, started_at TEXT, PRIMARY KEY(code, student_id)
    );
    CREATE TABLE IF NOT EXISTS goals (
        id {t['PK']}, student_id TEXT, goal TEXT, target_date TEXT, status TEXT DEFAULT 'active'
    )
    """)
    # Databases created by older versions (v1-v4.3, SQLite) lack some columns: add them in place.
    for table, column, definition in [
        ("student_preferences", "llm_model", "TEXT DEFAULT 'openai/gpt-oss-120b'"),
        ("student_preferences", "ui_color", "TEXT DEFAULT 'Blue'"),
        ("students", "role", "TEXT DEFAULT 'student'"),
        ("students", "last_active", "TEXT"),
        ("student_preferences", "theme_preset", "TEXT DEFAULT 'Default'"),
        ("student_preferences", "theme_bg", "TEXT DEFAULT ''"),
        ("student_preferences", "theme_text", "TEXT DEFAULT ''"),
        ("quiz_attempts", "shared_code", "TEXT"),
        ("quiz_attempts", "time_taken_sec", "INTEGER"),
        ("quiz_attempts", "time_limit_sec", "INTEGER DEFAULT 0"),
        ("quiz_attempts", "timed_out", "INTEGER DEFAULT 0"),
    ]:
        con.add_column(table, column, definition)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_attempt_shared ON quiz_attempts(shared_code, student_id) WHERE shared_code IS NOT NULL")
    # indexes for the queries the app runs on every page load
    con.execute("CREATE INDEX IF NOT EXISTS ix_qa_student ON question_attempts(student_id, id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_quiz_student ON quiz_attempts(student_id, id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_quiz_shared ON quiz_attempts(shared_code)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_mem_student ON memories(student_id, is_active)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_agent_student ON agent_sessions(student_id, id)")


def _m002_meta_and_vectors(con: Connection) -> None:
    t = _types(con)
    con.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
    # One embedding per memory row, stored in the database so vectors survive redeploys.
    con.execute(f"CREATE TABLE IF NOT EXISTS memory_vectors (memory_id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, dim INTEGER NOT NULL, embedding {t['BLOB']} NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_vec_student ON memory_vectors(student_id)")


def _m003_accounts(con: Connection) -> None:
    t = _types(con)
    con.execute(f"""CREATE TABLE IF NOT EXISTS users (
        id {t['PK']}, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student',
        student_id TEXT NOT NULL UNIQUE, display_name TEXT, failed_attempts INTEGER DEFAULT 0, locked_until TEXT,
        created_at TEXT NOT NULL, last_login_at TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS auth_sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_sessions_user ON auth_sessions(user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_quizzes_owner ON shared_quizzes(created_by)")


MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "baseline schema", _m001_baseline),
    (2, "app_meta + memory_vectors", _m002_meta_and_vectors),
    (3, "accounts + sessions", _m003_accounts),
]


def migrate() -> list[int]:
    """Apply every pending migration. Safe to call on every app start. Returns the versions applied."""
    applied_now: list[int] = []
    with connect() as con:
        if con.dialect == "postgres":
            con.execute("SELECT pg_advisory_xact_lock(?)", (_PG_LOCK_KEY,))
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
        done = {int(r[0]) for r in con.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, name, fn in MIGRATIONS:
            if version in done:
                continue
            fn(con)
            con.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)", (version, name, datetime.utcnow().isoformat()))
            applied_now.append(version)
    return applied_now


def current_version() -> int:
    with connect() as con:
        try:
            row = con.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()
            return int(row[0])
        except Exception:
            return 0
