"""One-time copy of an existing Prep AI SQLite file into the hosted Postgres database.

    export DATABASE_URL="postgresql://..."            # the NEW database (never commit this)
    python scripts/migrate_sqlite_to_postgres.py --source data/prep_ai.db --dry-run
    python scripts/migrate_sqlite_to_postgres.py --source data/prep_ai.db

* Safe to run twice: rows that already exist are skipped (ON CONFLICT DO NOTHING).
* The source file is opened read-only and never modified. Take a copy of it first anyway.
* Ends with a per-table row-count check and exits with an error if anything does not match.
* Memory embeddings are not copied: they are rebuilt automatically the first time each student logs in.
"""
import argparse, pathlib, sqlite3, sys, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "streamlit" not in sys.modules:  # config.py imports streamlit only for st.secrets; a stand-in keeps this script light
    try:
        import streamlit  # noqa: F401
    except Exception:
        m = types.ModuleType("streamlit"); m.secrets = {}; m.session_state = {}; sys.modules["streamlit"] = m

import db_core, migrations  # noqa: E402

# parents before children
TABLES = ["students", "student_preferences", "study_sessions", "questions", "quiz_attempts", "question_attempts", "mistakes",
          "mastery", "revision_schedule", "study_plans", "achievements", "memories", "agent_sessions", "merit_results",
          "shared_quizzes", "shared_quiz_starts", "goals", "daily_activity", "seen_questions",
          "bank_questions", "classes", "class_members", "assignments", "mock_results", "mock_progress",
          "daily_challenges", "daily_results", "live_sessions", "live_players", "live_answers", "reminder_prefs", "reminder_log", "ai_eval_runs"]
SERIAL = {"study_sessions", "questions", "quiz_attempts", "question_attempts", "mistakes", "study_plans", "achievements",
          "memories", "agent_sessions", "merit_results", "goals", "bank_questions", "classes", "assignments", "mock_results", "live_sessions", "reminder_log", "ai_eval_runs"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="path to the old SQLite file (data/prep_ai.db)")
    ap.add_argument("--dry-run", action="store_true", help="count rows only; write nothing")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--test-sqlite-target", action="store_true", help=argparse.SUPPRESS)  # used by the automated test only
    args = ap.parse_args()

    if not db_core.is_postgres() and not args.test_sqlite_target:
        print("DATABASE_URL must be a postgresql:// URL (the destination)."); return 2
    src_path = pathlib.Path(args.source)
    if not src_path.exists():
        print(f"Source file not found: {src_path}"); return 2
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True); src.row_factory = sqlite3.Row
    src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    print(f"Destination: {db_core.describe_backend()}")
    if not args.dry_run:
        print("Applying schema migrations:", migrations.migrate() or "already up to date")

    failures = 0
    for table in TABLES:
        if table not in src_tables:
            print(f"  {table:22} (not in source, skipped)"); continue
        src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if args.dry_run:
            print(f"  {table:22} {len(rows):>7} rows would be copied"); continue
        with db_core.connect() as con:
            dst_cols = con.columns(table)
            cols = [c for c in src_cols if c in dst_cols]
            sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) ON CONFLICT DO NOTHING"
            for i in range(0, len(rows), args.batch):
                con.executemany(sql, [[r[c] for c in cols] for r in rows[i:i + args.batch]])
            if table in SERIAL and con.dialect == "postgres":  # keep the id counter ahead of the copied ids
                con.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'), (SELECT COALESCE(MAX(id),1) FROM {table}), (SELECT COUNT(*)>0 FROM {table}))")
            n_dst = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        ok = n_dst >= len(rows)
        failures += 0 if ok else 1
        print(f"  {table:22} source {len(rows):>7}  destination {n_dst:>7}  {'OK' if ok else 'MISMATCH'}")
    print("\nDry run only - nothing written." if args.dry_run else ("\nDONE - all row counts match." if not failures else f"\n{failures} table(s) did not match - do NOT switch the app over."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
