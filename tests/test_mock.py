"""Mock test mode: blueprints, assembling from the bank (+AI top-up), scoring, history, resume."""
import pathlib, sys, tempfile, types, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
class _St(types.ModuleType):
    session_state = {}; secrets = {}
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
sys.modules["streamlit"] = _St("streamlit")
import config; config.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "m.db"
import bank, db, mock, progress  # noqa: E402
db.init_db()
n = [0]
def student():
    n[0] += 1; s = f"mk{n[0]}"; db.ensure_student(s, s); return s
def Q(subject, i, ans="A", tag="q"):
    return {"question": f"{subject} {tag} number {i} asks something unique?", "options": {"A": f"{subject} a{i}", "B": f"b{i}", "C": f"c{i}", "D": f"d{i}"}, "answer": ans, "explanation": "because", "concept": "c", "difficulty": "Medium"}
def fill(owner, subject, count, shared=False, tag="q"):
    for i in range(count): bank.add_question(owner, Q(subject, i, tag=tag), subject, "T", "manual", shared)

class Blueprints(unittest.TestCase):
    def test_full_paper_matches_the_published_pattern(self):
        b = mock.BLUEPRINTS["MDCAT-style full length (200 MCQs)"]
        self.assertEqual(sum(c for _, c in b["sections"]), 200); self.assertEqual(b["minutes"], 210)
        for name, bp in mock.BLUEPRINTS.items():
            self.assertTrue(abs(bp["minutes"] - mock.minutes_for(sum(c for _, c in bp["sections"]))) <= 3, name)

class Assemble(unittest.TestCase):
    def test_uses_own_and_tutor_shared_bank_but_not_private_tutor_questions(self):
        t, s = student(), student()
        fill(t, "Biology", 5, shared=True, tag="shared"); fill(t, "Biology", 5, shared=False, tag="private"); fill(s, "Biology", 3, tag="mine")
        out = mock.assemble(s, [("Biology", 20)], seed=1)
        texts = " ".join(q["question"] for q in out["questions"])
        self.assertEqual(len(out["questions"]), 8); self.assertNotIn("private", texts); self.assertIn("shared", texts); self.assertIn("mine", texts)
        self.assertEqual(out["report"]["Biology"], {"wanted": 20, "bank": 8, "ai": 0, "missing": 12})

    def test_unseen_questions_come_first(self):
        s = student(); fill(s, "Physics", 10)
        seen = [Q("Physics", i) for i in range(6)]
        db.record_quiz(s, "Physics", "T", seen, {0: "A"}, "Medium")
        got = mock.assemble(s, [("Physics", 4)], seed=3, shuffle_opts=False)["questions"]
        self.assertTrue(all(int(q["question"].split("number ")[1].split(" ")[0]) >= 6 for q in got))

    def test_ai_tops_up_shortfall_saves_to_bank_and_respects_budget(self):
        s = student(); fill(s, "Chemistry", 2); asked = []
        def gen(subject, k):
            asked.append((subject, k)); return [Q(subject, 100 + i, tag="ai") for i in range(k)]
        out = mock.assemble(s, [("Chemistry", 6), ("English", 6)], seed=2, generate=gen, max_ai=7)
        self.assertEqual(asked, [("Chemistry", 4), ("English", 3)])                           # 4 + 3 = the 7-question budget
        self.assertEqual(out["report"]["Chemistry"]["ai"], 4); self.assertEqual(out["report"]["English"], {"wanted": 6, "bank": 0, "ai": 3, "missing": 3})
        self.assertEqual(bank.available_for_student(s, "Chemistry"), 6)                         # AI questions were kept for the next mock

    def test_ai_failure_is_not_fatal(self):
        s = student(); fill(s, "Biology", 2)
        out = mock.assemble(s, [("Biology", 5)], generate=lambda sub, k: (_ for _ in ()).throw(RuntimeError("rate limited")))
        self.assertEqual((len(out["questions"]), out["report"]["Biology"]["missing"]), (2, 3))

    def test_option_shuffle_keeps_the_right_answer_and_is_stable_per_seed(self):
        s = student(); fill(s, "Biology", 12)
        a = mock.assemble(s, [("Biology", 12)], seed="x")["questions"]; b = mock.assemble(s, [("Biology", 12)], seed="x")["questions"]
        self.assertEqual([q["question"] for q in a], [q["question"] for q in b]); self.assertEqual([q["options"] for q in a], [q["options"] for q in b])
        self.assertTrue(any(q["answer"] != "A" for q in a))                                    # some answers moved...
        for q in a:                                                                            # ...but each still points at the original correct text
            self.assertTrue(q["options"][q["answer"]].startswith("Biology a"))
        self.assertTrue(all(q["section"] == "Biology" for q in a))

class Scoring(unittest.TestCase):
    QS = [{"question": f"q{i}", "answer": "A", "section": sec} for i, sec in enumerate(["Biology"] * 4 + ["Physics"] * 3 + ["English"] * 3)]
    def test_sections_totals_and_negative_marking(self):
        ans = {0: "A", 1: "A", 2: "B", 4: "B", 5: "B", 6: "B", 7: "A"}           # Bio 2 right 1 wrong 1 skip; Phy 0 right 3 wrong; Eng 1 right 2 skip
        plain = mock.section_scores(self.QS, ans, False); neg = mock.section_scores(self.QS, ans, True)
        self.assertEqual([r["section"] for r in plain["sections"]], ["Biology", "Physics", "English"])
        self.assertEqual((plain["correct"], plain["wrong"], plain["skipped"], plain["total"]), (3, 4, 3, 10)); self.assertEqual(plain["marks"], 3.0)
        self.assertEqual(neg["marks"], 3 - 4 * 0.25); self.assertEqual(neg["sections"][1]["marks"], -0.75)
        bio = plain["sections"][0]; self.assertEqual((bio["accuracy"], bio["percent"]), (66.7, 50.0))
    def test_advice(self):
        ans = {0: "A", 1: "A", 2: "A", 3: "A", 4: "B", 5: "B", 6: "B"}
        tips = " ".join(mock.focus_advice(mock.section_scores(self.QS, ans, True)))
        self.assertIn("Weakest section", tips); self.assertIn("Physics", tips); self.assertIn("skipped", tips)

class Persistence(unittest.TestCase):
    def test_history_roundtrip(self):
        s = student(); sc = mock.section_scores(Scoring.QS, {0: "A"}, False)
        mock.save_result(s, None, "Quick mock", sc, 600)
        h = mock.history(s); self.assertEqual((len(h), h[0]["blueprint"], h[0]["percent"], len(h[0]["sections"])), (1, "Quick mock", 10.0, 3))
        self.assertEqual(mock.history(student()), [])
    def test_resume_state_survives_a_reload(self):
        s = student(); quiz = {"uid": "u1", "questions": Scoring.QS, "subject": "Mock Test", "topic": "Quick", "difficulty": "Mixed", "time_limit_sec": 3000,
                               "started_at": 1000.5, "answers": {0: "A", 7: "C"}, "answer_ts": {0: 1010.0, 7: 1020.0}, "negative": True, "blueprint": "Quick mock", "mock": True}
        self.assertIsNone(mock.load_progress(s))
        mock.save_progress(s, quiz); quiz["answers"][1] = "B"; mock.save_progress(s, quiz)               # saving twice = upsert
        back = mock.load_progress(s)
        self.assertEqual((back["answers"], back["started_at"], back["time_limit_sec"], back["negative"]), ({0: "A", 7: "C", 1: "B"}, 1000.5, 3000, True))
        self.assertEqual(back["answer_ts"][7], 1020.0); self.assertIsNone(back["result"]); self.assertEqual(len(back["questions"]), 10)
        self.assertIsNone(mock.load_progress(student()))
        mock.clear_progress(s); self.assertIsNone(mock.load_progress(s))

class MasteryCredit(unittest.TestCase):
    def test_each_question_counts_for_its_own_subject(self):
        s = student(); qs = [dict(Q("Biology", 1), subject="Biology"), dict(Q("Physics", 1), subject="Physics")]
        db.record_quiz(s, "Mock Test", "Quick mock", qs, {0: "A", 1: "B"}, "Mixed")
        subjects = {t["subject"] for t in db.topic_mastery(s)}
        self.assertEqual(subjects, {"Biology", "Physics"})

if __name__ == "__main__":
    unittest.main(verbosity=1)
