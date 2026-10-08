"""Streaks, daily goal, XP/levels, no-repeat questions, retakeable mistakes."""
import pathlib, sys, tempfile, types, unittest
from datetime import date, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
class _St(types.ModuleType):
    session_state = {}; secrets = {}
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
sys.modules["streamlit"] = _St("streamlit")
for name in ("groq", "ddgs", "faiss", "sentence_transformers"):
    m = types.ModuleType(name); m.Groq = object; m.DDGS = object; m.SentenceTransformer = object; sys.modules[name] = m
import config; config.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "p.db"
import db, progress, agent_system  # noqa: E402
db.init_db()
D = date(2026, 10, 8)
n = [0]
def sid():
    n[0] += 1; s = f"pg{n[0]}"; db.ensure_student(s, s); return s
def Q(i, ok="A", extra=""):
    return {"question": f"What is fact number {i}{extra}?", "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "answer": ok, "concept": "c", "explanation": "e", "difficulty": "Medium"}

class Streaks(unittest.TestCase):
    def d(self, *offs): return [D - timedelta(days=o) for o in offs]
    def test_cases(self):
        s = progress.streaks
        self.assertEqual(s([], D), (0, 0))
        self.assertEqual(s(self.d(0), D), (1, 1))
        self.assertEqual(s(self.d(0, 1, 2), D), (3, 3))
        self.assertEqual(s(self.d(1, 2, 3), D), (3, 3))              # not yet active today: still alive until midnight
        self.assertEqual(s(self.d(2, 3, 4), D), (0, 3))              # missed yesterday: broken, best kept
        self.assertEqual(s(self.d(0, 1, 5, 6, 7, 8), D), (2, 4))     # gap resets, best is the older run
        self.assertEqual(s(self.d(0, 0, 1), D), (2, 2))              # duplicates ignored

class Xp(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(progress.level_info(0)["level"], 1); self.assertEqual(progress.level_info(49)["level"], 1)
        self.assertEqual(progress.level_info(50)["level"], 2); self.assertEqual(progress.level_info(199)["level"], 2); self.assertEqual(progress.level_info(200)["level"], 3)
        li = progress.level_info(120); self.assertEqual((li["xp_into_level"], li["xp_for_next"]), (70, 150))
    def test_xp_formula(self):
        self.assertEqual(progress.xp_for(10, 7), 10 * 2 + 7 * 3 + 10); self.assertEqual(progress.xp_for(10, 10, perfect=True), 20 + 30 + 10 + 20)

class Activity(unittest.TestCase):
    def test_quiz_updates_progress_goal_and_week(self):
        s = sid(); p0 = progress.get_progress(s)
        self.assertEqual((p0["streak"], p0["total_xp"], p0["today_questions"], p0["goal"]), (0, 0, 0, 20))
        qs = [Q(i) for i in range(10)]
        db.record_quiz(s, "Biology", "Cells", qs, {i: "A" for i in range(8)} | {8: "B"}, "Medium")      # 8 right, 1 wrong, 1 skipped
        p = progress.get_progress(s)
        self.assertEqual((p["streak"], p["today_questions"], p["total_xp"]), (1, 9, 9 * 2 + 8 * 3 + 10))
        self.assertFalse(p["goal_done"]); self.assertEqual(p["goal_pct"], 45.0); self.assertEqual(p["week"][-1]["questions"], 9); self.assertEqual(len(p["week"]), 7)
        db.record_quiz(s, "Biology", "Cells", [Q(i, extra="b") for i in range(12)], {i: "A" for i in range(12)}, "Medium")
        p2 = progress.get_progress(s); self.assertTrue(p2["goal_done"])
        ms = progress.new_milestones(s, p, p2); self.assertTrue(any(t == "Daily goal reached" for _, t in ms))
        self.assertEqual(progress.set_daily_goal(s, 3), 5); self.assertEqual(progress.set_daily_goal(s, 500), 200); self.assertEqual(progress.get_daily_goal(s), 200)

    def test_levelup_and_streak_milestones(self):
        a = {"streak": 2, "level": 1, "goal_done": True}; b = {"streak": 3, "level": 2, "goal_done": True}
        got = {c for c, _ in progress.new_milestones("x", a, b)}; self.assertEqual(got, {"streak_3", "level_2"})
        self.assertEqual(progress.new_milestones("x", b, b), [])

    def test_students_are_isolated(self):
        a, b = sid(), sid(); db.record_quiz(a, "Biology", "Cells", [Q(1)], {0: "A"}, "Medium")
        self.assertEqual(progress.get_progress(b)["total_xp"], 0)

class NoRepeat(unittest.TestCase):
    def test_hash_ignores_case_and_punctuation(self):
        self.assertEqual(progress.qhash("What is DNA?"), progress.qhash("  what is  dna "))
    def test_questions_seen_after_a_quiz(self):
        s = sid(); db.record_quiz(s, "Biology", "Cells", [Q(1), Q(2)], {0: "A"}, "Medium")
        seen = progress.seen_hashes(s); self.assertEqual(len(seen), 2)
        fresh, stale = progress.pick_fresh([Q(1), Q(3), Q(3), Q(4)], seen, [])
        self.assertEqual([q["question"] for q in fresh], [Q(3)["question"], Q(4)["question"]]); self.assertEqual(len(stale), 1)
        self.assertEqual(len(progress.recent_seen_texts(s, "Biology", "Cells")), 2)

    def _orch(self, batches):
        o = object.__new__(agent_system.Orchestrator)
        calls = []
        class MemA: retrieve = staticmethod(lambda q: [])
        class Ass:
            def generate_mcqs(self, ctx, memories, count, avoid=None): calls.append((count, list(avoid or []))); return batches[len(calls) - 1]
            def validate(self, qs, ctx, topic): return qs
        o.memory_agent, o.assessment = MemA(), Ass()
        return o, calls

    def test_generation_asks_again_and_never_returns_repeats_when_enough_new_exist(self):
        s = sid(); db.record_quiz(s, "Biology", "Cells", [Q(1), Q(2)], {0: "A"}, "Medium")
        o, calls = self._orch([[Q(1), Q(2), Q(3)], [Q(4), Q(5)]])
        got = o.practice_request(agent_system.AgentContext(student_id=s, request="r", subject="Biology", topic="Cells"), 3)
        self.assertEqual([q["question"] for q in got], [Q(3)["question"], Q(4)["question"], Q(5)["question"]])
        self.assertEqual(calls[0][0], 3); self.assertEqual(calls[1][0], 2)                  # second ask only for the missing 2
        self.assertTrue(any("fact number 1" in t for t in calls[1][1]))                      # and tells the model what to avoid

    def test_small_topic_tops_up_with_repeats_instead_of_a_tiny_quiz(self):
        s = sid(); db.record_quiz(s, "Biology", "Cells", [Q(1), Q(2)], {0: "A"}, "Medium")
        o, _ = self._orch([[Q(1), Q(2), Q(3)], [Q(1), Q(2)]])
        got = o.practice_request(agent_system.AgentContext(student_id=s, request="r", subject="Biology", topic="Cells"), 3)
        self.assertEqual(len(got), 3); self.assertEqual(got[0]["question"], Q(3)["question"])      # the fresh one comes first

class Retake(unittest.TestCase):
    def test_wrong_answers_come_back_as_real_mcqs_until_fixed(self):
        s = sid()
        db.record_quiz(s, "Biology", "Cells", [Q(1), Q(2), Q(3)], {0: "B", 1: "A", 2: "C"}, "Medium")      # Q1 and Q3 wrong
        r = progress.retakeable_mistakes(s)
        self.assertEqual({q["question"] for q in r}, {Q(1)["question"], Q(3)["question"]})
        self.assertEqual(set(r[0]["options"]), {"A", "B", "C", "D"}); self.assertEqual(r[0]["answer"], "A")
        db.record_quiz(s, "Biology", "Cells", [Q(1)], {0: "A"}, "Medium")                                  # now answered correctly
        self.assertEqual({q["question"] for q in progress.retakeable_mistakes(s)}, {Q(3)["question"]})
    def test_old_mistakes_without_options_are_skipped(self):
        s = sid()
        with db.connect() as con:
            con.insert("INSERT INTO mistakes(student_id,subject,topic,concept,question_text,wrong_answer,correct_answer,explanation,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (s, "Biology", "Cells", "c", "Old question?", "B", "A", "e", "2026-01-01"))
        self.assertEqual(progress.retakeable_mistakes(s), [])

if __name__ == "__main__":
    unittest.main(verbosity=1)
