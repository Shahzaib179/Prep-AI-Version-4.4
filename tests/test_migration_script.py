"""Checks scripts/migrate_sqlite_to_postgres.py end to end (copy, idempotency, counts) using SQLite as the stand-in destination."""
import os, pathlib, subprocess, sys, tempfile, sqlite3

ROOT = pathlib.Path(__file__).resolve().parent.parent
tmp = pathlib.Path(tempfile.mkdtemp())
src, dst = tmp / "old.db", tmp / "new.db"

# build a realistic v4.3-shaped source using the real (old) schema bootstrap
env = dict(os.environ, DATABASE_URL=f"sqlite:///{src}")
boot = f"""
import sys, types; sys.path.insert(0, {str(ROOT)!r})
m = types.ModuleType('streamlit'); m.secrets = {{}}; m.session_state = {{}}; sys.modules['streamlit'] = m
import db; db.init_db()
db.ensure_student('s1','Ali'); db.ensure_student('s2','Sara')
QS=[{{'question':f'Q{{i}}','options':{{'A':'a'}},'answer':'A','concept':'c','explanation':'e'}} for i in range(4)]
db.record_quiz('s1','Biology','Cells',QS,{{0:'A',1:'A'}},'Medium'); db.record_quiz('s2','Biology','Cells',QS,{{0:'A'}},'Medium')
code=db.create_shared_quiz('s1','T','Biology','Cells','Medium',QS,0)
db.save_agent_session('s1','Tutor','hi','hello')
"""
subprocess.run([sys.executable, "-W", "ignore", "-c", boot], check=True, env=env)

env2 = dict(os.environ, DATABASE_URL=f"sqlite:///{dst}")
cmd = [sys.executable, "-W", "ignore", str(ROOT / "scripts" / "migrate_sqlite_to_postgres.py"), "--source", str(src), "--test-sqlite-target"]
first = subprocess.run(cmd, env=env2, capture_output=True, text=True); print(first.stdout[-900:]); assert first.returncode == 0, first.stdout + first.stderr
second = subprocess.run(cmd, env=env2, capture_output=True, text=True); assert second.returncode == 0, second.stdout + second.stderr   # idempotent
a, b = sqlite3.connect(src), sqlite3.connect(dst)
for t in ("students", "quiz_attempts", "question_attempts", "mastery", "shared_quizzes", "agent_sessions"):
    na, nb = a.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], b.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert na == nb and na > 0, (t, na, nb)
dry = subprocess.run(cmd + ["--dry-run"], env=dict(os.environ, DATABASE_URL=f"sqlite:///{tmp/'none.db'}"), capture_output=True, text=True); assert dry.returncode == 0
print("MIGRATION SCRIPT TEST PASSED (copy, re-run, counts, dry-run)")
