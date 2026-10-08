"""Database contract tests - the SAME tests run on SQLite (default) and on Postgres.

SQLite (no setup):          python tests/test_db_contract.py
Postgres (staging DB only!): DATABASE_URL=postgresql://... CONTRACT_ALLOW_RESET=yes python tests/test_db_contract.py

On Postgres the tests create rows whose ids start with 'ct_' and delete them again; they never touch other data.
No Streamlit, FAISS, embedding model or API key is needed (light stand-ins are installed first).
"""
import hashlib, os, pathlib, sys, tempfile, types, unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np

class _St(types.ModuleType):
    session_state = {}; secrets = {}
    def __getattr__(self, name):
        if name.startswith("__"): raise AttributeError(name)
        return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
sys.modules["streamlit"] = _St("streamlit")
for name in ("groq", "ddgs", "sentence_transformers", "faiss"):
    m = types.ModuleType(name); m.Groq = object; m.DDGS = object; m.SentenceTransformer = object; sys.modules[name] = m

TMP = pathlib.Path(tempfile.mkdtemp())
import config
config.DB_PATH = TMP / "contract.db"
import db, db_core, memory, migrations, progress  # noqa: E402

REMOTE = db_core.is_postgres()
if REMOTE and os.environ.get("CONTRACT_ALLOW_RESET") != "yes":
    sys.exit("Refusing to run against Postgres without CONTRACT_ALLOW_RESET=yes (use a STAGING database).")

def _fake_embed(texts):
    out = []
    for t in texts:
        v = np.frombuffer(hashlib.md5(t.encode()).digest(), dtype=np.uint8).astype("float32") - 127.5
        out.append(v / np.linalg.norm(v))
    return np.vstack(out).astype("float32")
memory.embed_texts = _fake_embed

QS = [{"question": f"Q{i}", "options": {"A": "a", "B": "b"}, "answer": "A", "concept": "c", "explanation": "e"} for i in range(4)]
IDS = ["ct_a", "ct_b", "ct_tutor", "ct_m1", "ct_m2"]

def wipe():
    with db_core.connect() as con:
        for t in ("question_attempts", "mistakes", "mastery", "revision_schedule", "quiz_attempts", "achievements", "agent_sessions", "memory_vectors", "memories", "merit_results", "student_preferences", "daily_activity", "seen_questions"):
            con.execute(f"DELETE FROM {t} WHERE student_id LIKE 'ct\\_%' ESCAPE '\\'")
        con.execute("DELETE FROM mock_results WHERE student_id LIKE 'ct\\_%' ESCAPE '\\'")
        con.execute("DELETE FROM mock_progress WHERE student_id LIKE 'ct\\_%' ESCAPE '\\'")
        con.execute("DELETE FROM shared_quiz_starts WHERE student_id LIKE 'ct\\_%' ESCAPE '\\'")
        con.execute("DELETE FROM shared_quizzes WHERE created_by LIKE 'ct\\_%' ESCAPE '\\'")
        con.execute("DELETE FROM students WHERE id LIKE 'ct\\_%' ESCAPE '\\'")
    memory._CACHE.clear()


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self): wipe()
    tearDown = setUp

    def test_migrations_recorded_and_idempotent(self):
        self.assertEqual(migrations.migrate(), [])                                     # second run applies nothing
        self.assertEqual(migrations.current_version(), max(v for v, _, _ in migrations.MIGRATIONS))

    def test_insert_returns_ids_and_integrity_error_is_portable(self):
        db.ensure_student("ct_a", "Ali")
        with db_core.connect() as con:
            first = con.insert("INSERT INTO goals(student_id,goal) VALUES(?,?)", ("ct_a", "x"))
            second = con.insert("INSERT INTO goals(student_id,goal) VALUES(?,?)", ("ct_a", "y"))
        self.assertGreater(second, first)
        with db_core.connect() as con: con.execute("DELETE FROM goals WHERE student_id='ct_a'")
        qid = db.record_quiz("ct_a", "Biology", "Cells", QS, {0: "A"}, "Medium", shared_code="CTTEST", time_taken_sec=5)
        self.assertGreater(qid, 0)
        with self.assertRaises(db_core.IntegrityError):                                # one attempt per student per shared quiz
            db.record_quiz("ct_a", "Biology", "Cells", QS, {0: "A"}, "Medium", shared_code="CTTEST")

    def test_quiz_mastery_and_dashboard_roundtrip(self):
        db.ensure_student("ct_a", "Ali")
        db.record_quiz("ct_a", "Biology", "Cells", QS, {0: "A", 1: "A", 2: "B"}, "Medium")
        h = db.history("ct_a"); self.assertEqual(len(h), 1); self.assertEqual((h[0]["correct"], h[0]["incorrect"], h[0]["skipped"]), (2, 1, 1))
        t = db.topic_mastery("ct_a"); self.assertEqual(len(t), 1); self.assertEqual(t[0]["attempts"], 3)   # skipped questions are not counted as attempts
        self.assertEqual(db.dashboard_stats("ct_a")["attempted"], 3)
        self.assertEqual(len(db.recent_mistakes("ct_a")), 1)
        self.assertTrue(db.weak_topics("ct_a") is not None)
        self.assertEqual(len(db.revision_recommendations("ct_a")), 1)
        db.rebuild_mastery("ct_a"); db.rebuild_mastery("ct_a")                          # upsert path is repeatable
        self.assertEqual(len(db.topic_mastery("ct_a")), 1)

    def test_shared_quiz_flow_and_roster(self):
        db.ensure_student("ct_tutor", "Tutor"); db.set_role("ct_tutor", "tutor")
        db.ensure_student("ct_a", "ali"); db.ensure_student("ct_b", "Bilal")
        code = db.create_shared_quiz("ct_tutor", "T", "Biology", "Cells", "Medium", QS, 600)
        self.assertIsNotNone(db.get_shared_quiz(code))
        s1 = db.shared_quiz_start(code, "ct_a"); self.assertEqual(s1, db.shared_quiz_start(code, "ct_a"))   # start time never moves
        db.record_quiz("ct_a", "Biology", "Cells", QS, {0: "A", 1: "A"}, "Medium", shared_code=code, time_taken_sec=30)
        db.record_shared_timeout("ct_b", {"questions": QS, "subject": "Biology", "topic": "Cells", "difficulty": "Medium", "code": code, "time_limit_sec": 600}, 600)
        res = db.shared_quiz_results(code); self.assertEqual([r["student_id"] for r in res], ["ct_a", "ct_b"])
        listing = [q for q in db.list_shared_quizzes("ct_tutor") if q["code"] == code][0]
        self.assertEqual(listing["attempts"], 2); self.assertEqual(listing["avg_score"], 25.0)
        ids = [r["student_id"] for r in db.roster() if r["student_id"].startswith("ct_")]
        self.assertEqual(ids, ["ct_a", "ct_b"])                                         # tutor hidden, sorted case-insensitively
        db.set_shared_quiz_open(code, False); self.assertEqual(db.get_shared_quiz(code)["is_open"], 0)

    def test_achievements_preferences_sessions_merit(self):
        db.ensure_student("ct_a", "Ali"); db.ensure_student("ct_a", "Ali B")            # idempotent
        self.assertEqual(db.get_student("ct_a")["name"], "Ali B")
        db.add_achievement("ct_a", "first", "First"); db.add_achievement("ct_a", "first", "First")
        self.assertEqual(len(db.achievements("ct_a")), 1)
        db.update_preferences("ct_a", ui_color="Green"); self.assertEqual(db.get_preferences("ct_a")["ui_color"], "Green")
        db.save_agent_session("ct_a", "Tutor Agent", "hi", "hello"); self.assertEqual(len(db.get_agent_sessions("ct_a")), 1)
        db.save_merit_result("ct_a", "MDCAT", "f1", "F", "MBBS", {"a": 1}, 80.5); self.assertEqual(db.merit_results("ct_a")[0]["marks"], {"a": 1})
        self.assertEqual(db.delete_agent_sessions("ct_a"), 1)

    def test_memory_vectors_survive_a_restart(self):
        db.ensure_student("ct_m1", "M")
        m = memory.LongTermMemory("ct_m1")
        for i in range(3): m.add(f"memory number {i} about photosynthesis", "learning")
        memory._CACHE.clear()                                                           # simulate a fresh server process
        again = memory.LongTermMemory("ct_m1")
        self.assertEqual((again.index.ntotal, len(again.metadata)), (3, 3))
        self.assertTrue(again.retrieve("photosynthesis", 2))
        self.assertGreater(again.usage()["vector_bytes"], 0)

    def test_memories_without_vectors_are_re_embedded_once(self):
        """Data coming from v4.3 has memory rows but the vector files were lost."""
        db.ensure_student("ct_m1", "M"); now = datetime.utcnow().isoformat()
        with db_core.connect() as con:
            for i in range(2):
                con.insert("INSERT INTO memories(student_id,memory_type,content,created_at,updated_at,last_used_at) VALUES(?,?,?,?,?,?)", ("ct_m1", "learning", f"legacy memory {i} about cells", now, now, now))
        m = memory.LongTermMemory("ct_m1")
        self.assertEqual(m.index.ntotal, 2)
        with db_core.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM memory_vectors WHERE student_id='ct_m1'").fetchone()[0], 2)

    def test_memory_isolation_and_reset(self):
        db.ensure_student("ct_m1", "A"); db.ensure_student("ct_m2", "B")
        memory.LongTermMemory("ct_m1").add("fact for student one here", "learning"); memory.LongTermMemory("ct_m2").add("fact for student two here", "learning")
        memory.reset_memory("ct_m1")
        self.assertEqual(memory.LongTermMemory("ct_m1").count(), 0); self.assertEqual(memory.LongTermMemory("ct_m2").count(), 1)
        with db_core.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM memory_vectors WHERE student_id='ct_m1'").fetchone()[0], 0)

    def test_progress_upsert_streak_goal_seen_and_retake(self):
        db.ensure_student("ct_a", "Ali")
        db.record_quiz("ct_a", "Biology", "Cells", QS, {0: "A", 1: "B"}, "Medium")
        db.record_quiz("ct_a", "Biology", "Cells", [dict(q, question="Other " + q["question"]) for q in QS], {0: "A", 1: "A", 2: "A"}, "Medium")   # 2nd quiz same day -> ON CONFLICT DO UPDATE
        p = progress.get_progress("ct_a"); self.assertEqual((p["streak"], p["today_questions"]), (1, 5))
        self.assertEqual(progress.set_daily_goal("ct_a", 10), 10); self.assertEqual(progress.get_daily_goal("ct_a"), 10)
        self.assertEqual(len(progress.seen_hashes("ct_a")), 8)
        QS_OPT = [dict(q, options={"A": "a", "B": "b", "C": "c", "D": "d"}) for q in QS]
        db.record_quiz("ct_a", "Chemistry", "Atoms", [dict(q, question="Atoms " + q["question"]) for q in QS_OPT], {0: "B"}, "Medium")
        self.assertEqual(len(progress.retakeable_mistakes("ct_a")), 2)   # the A/B-only question from quiz 1 + the new one

    def test_rollback_on_error(self):
        db.ensure_student("ct_a", "Ali")
        with self.assertRaises(RuntimeError):
            with db_core.connect() as con:
                con.execute("UPDATE students SET name=? WHERE id=?", ("Changed", "ct_a"))
                raise RuntimeError("boom")
        self.assertEqual(db.get_student("ct_a")["name"], "Ali")


class Translator(unittest.TestCase):
    def test_placeholders_and_literal_percent(self):
        t = db_core.translate_for_postgres
        self.assertEqual(t("SELECT * FROM x WHERE a=? AND b=?"), "SELECT * FROM x WHERE a=%s AND b=%s")
        self.assertEqual(t("SELECT '?' , a FROM x WHERE a=?"), "SELECT '?' , a FROM x WHERE a=%s")   # ? inside a string literal is kept
        self.assertEqual(t("SELECT * FROM x WHERE n LIKE '50%' AND a=?"), "SELECT * FROM x WHERE n LIKE '50%%' AND a=%s")

    def test_pg_row_behaves_like_sqlite_row(self):
        r = db_core.PgRow(["a", "b"], [1, "x"])
        self.assertEqual((r[0], r["b"], len(r), dict(r)), (1, "x", 2, {"a": 1, "b": "x"}))
        with self.assertRaises(IndexError): r["zzz"]

    def test_sql_in_db_py_has_no_sqlite_only_constructs(self):
        src = (ROOT / "db.py").read_text() + (ROOT / "memory.py").read_text() + (ROOT / "tutor_pages.py").read_text()
        for extra in ("bank.py", "classes.py", "mock.py", "progress.py", "auth.py", "class_pages.py", "mock_pages.py", "bank_pages.py"):
            src += (ROOT / extra).read_text()
        for bad in ("date('now')", "INSERT OR IGNORE", "INSERT OR REPLACE", "COLLATE NOCASE", "lastrowid", "AUTOINCREMENT"):
            self.assertNotIn(bad, src, f"{bad} is SQLite-only and breaks Postgres")


if __name__ == "__main__":
    unittest.main(verbosity=2)
