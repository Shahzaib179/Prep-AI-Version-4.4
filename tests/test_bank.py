"""Question tools (validate / shuffle / CSV) and the question bank."""
import pathlib, sys, tempfile, types, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
class _St(types.ModuleType):
    session_state = {}; secrets = {}
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
sys.modules["streamlit"] = _St("streamlit")
import config; config.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "b.db"
import db, bank, progress, question_tools as qt  # noqa: E402
db.init_db()

def Q(i=1, ans="B", **kw):
    q = {"question": f"Which organelle performs function number {i}?", "options": {"A": f"alpha{i}", "B": f"beta{i}", "C": f"gamma{i}", "D": f"delta{i}"}, "answer": ans, "explanation": "because", "concept": "cell", "difficulty": "Medium"}
    q.update(kw); return q

class Validation(unittest.TestCase):
    def test_good_and_bad(self):
        self.assertEqual(qt.validate_question(Q()), [])
        self.assertTrue(qt.validate_question(Q(question="short")))
        self.assertTrue(qt.validate_question(Q(answer="E")))
        self.assertTrue(qt.validate_question(Q(options={"A": "x", "B": "y", "C": "z"})))
        self.assertTrue(qt.validate_question(Q(options={"A": "same", "B": "SAME", "C": "z", "D": "w"})))
        self.assertTrue(qt.validate_question(Q(options={"A": "x", "B": "", "C": "z", "D": "w"})))
    def test_normalize(self):
        n = qt.normalize_question(Q(answer=" b ", question="  spaced   out question text  "))
        self.assertEqual((n["answer"], n["question"]), ("B", "spaced out question text"))

class Shuffle(unittest.TestCase):
    def test_answer_text_is_preserved_for_many_seeds(self):
        for seed in range(200):
            q = Q(7, ans="C"); s = qt.shuffle_options(q, seed)
            self.assertEqual(s["options"][s["answer"]], q["options"]["C"])                      # correct text still marked correct
            self.assertEqual(sorted(s["options"].values()), sorted(q["options"].values())); self.assertEqual(list(s["options"]), ["A", "B", "C", "D"])
    def test_deterministic_and_actually_varies(self):
        q = Q(); self.assertEqual(qt.shuffle_options(q, "x:1"), qt.shuffle_options(q, "x:1"))
        orders = {tuple(qt.shuffle_options(q, f"s{i}")["options"].values()) for i in range(50)}; self.assertGreater(len(orders), 10)
        pos = [list(qt.shuffle_options(q, f"p{i}")["options"]).index(qt.shuffle_options(q, f"p{i}")["answer"]) for i in range(400)]
        self.assertTrue(all(60 < pos.count(k) < 140 for k in range(4)))                          # answer letter spread evenly
    def test_pointer_options_are_never_moved(self):
        for opts in ({"A": "x1", "B": "y1", "C": "z1", "D": "All of the above"}, {"A": "x", "B": "y", "C": "Both A and B", "D": "w"},
                     {"A": "x", "B": "y", "C": "z", "D": "None of the above"}, {"A": "x", "B": "y", "C": "A and B", "D": "w"}, {"A": "x", "B": "y", "C": "Option C only", "D": "w"}):
            q = Q(options=opts); self.assertTrue(qt.shuffle_blocked(q)); self.assertEqual(qt.shuffle_options(q, 5), q)
        self.assertFalse(qt.shuffle_blocked(Q(options={"A": "above 60 degrees", "B": "below 30", "C": "equal", "D": "none"})))
    def test_original_not_mutated(self):
        q = Q(); before = dict(q["options"]); qt.shuffle_options(q, 1); self.assertEqual(q["options"], before)

class Csv(unittest.TestCase):
    def test_roundtrip_and_errors(self):
        rows = [dict(Q(1), subject="Biology", topic="Cells"), dict(Q(2, ans="A"), subject="Physics", topic="Waves, light")]
        text = qt.to_csv(rows); good, errs = qt.parse_csv(text)
        self.assertEqual((len(good), errs), (2, [])); self.assertEqual(good[1]["topic"], "Waves, light"); self.assertEqual(good[0]["options"], rows[0]["options"])
        bad = text + 'Biology,Cells,Easy,tiny,a,b,c,d,Z,,\n'
        good, errs = qt.parse_csv(bad); self.assertEqual(len(good), 2); self.assertEqual(len(errs), 1); self.assertIn("Row 4", errs[0])
    def test_aliases_bom_and_missing_columns(self):
        text = "﻿Question_Text,Option_A,Option_B,Option_C,Option_D,Correct\nWhat does the nucleus contain?,DNA,Lipids,Water,Salt,A\n"
        good, errs = qt.parse_csv(text, "Biology", "Cells"); self.assertEqual((len(good), errs), (1, [])); self.assertEqual((good[0]["subject"], good[0]["answer"]), ("Biology", "A"))
        self.assertTrue(qt.parse_csv("foo,bar\n1,2\n")[1]); self.assertTrue(qt.parse_csv("")[1])

class Bank(unittest.TestCase):
    def setUp(self):
        db.ensure_student("t1", "T1"); db.ensure_student("t2", "T2"); db.ensure_student("stu", "Stu")
    def test_add_dedupe_validate_isolation(self):
        a = bank.add_question("t1", Q(1), "Biology", "Cells"); self.assertEqual(a[1], "added")
        self.assertEqual(bank.add_question("t1", Q(1, ans="C"), "Biology", "Cells")[1], "duplicate")        # same text
        self.assertEqual(bank.add_question("t2", Q(1), "Biology", "Cells")[1], "added")                    # other owner is independent
        self.assertTrue(bank.add_question("t1", Q(2, answer="Z"), "Biology")[1].startswith("invalid"))
        self.assertIsNone(bank.get_question("t2", a[0])); self.assertEqual(bank.get_question("t1", a[0])["subject"], "Biology")
    def test_edit_delete_owner_checked_and_clash(self):
        i1, _ = bank.add_question("t1", Q(11), "Biology", "Cells"); i2, _ = bank.add_question("t1", Q(12), "Biology", "Cells")
        ok, msg = bank.update_question("t1", i1, dict(Q(11), answer="D", explanation="new")); self.assertTrue(ok)
        self.assertEqual(bank.get_question("t1", i1)["answer"], "D")
        self.assertFalse(bank.update_question("t2", i1, Q(11))[0])                                         # not the owner
        self.assertFalse(bank.update_question("t1", i1, Q(12))[0])                                         # would duplicate another question
        self.assertFalse(bank.update_question("t1", i1, Q(11, answer="Q"))[0])
        self.assertEqual(bank.delete_questions("t2", [i1, i2]), 0); self.assertEqual(bank.delete_questions("t1", [i1, i2]), 2)
    def test_filters_counts_and_pagination(self):
        for i in range(30, 38): bank.add_question("t2", Q(i, difficulty="Hard" if i % 2 else "Easy"), "Chemistry", f"Bonds {i}")
        bank.add_question("t2", Q(99), "Physics", "Waves")
        self.assertEqual(bank.count_questions("t2", subject="Chemistry"), 8); self.assertEqual(bank.count_questions("t2", subject="Chemistry", difficulty="Hard"), 4)
        self.assertEqual(len(bank.list_questions("t2", subject="Chemistry", limit=5)), 5); self.assertEqual(len(bank.list_questions("t2", subject="Chemistry", limit=5, offset=5)), 3)
        self.assertEqual(bank.count_questions("t2", topic="bonds 3"), 8); self.assertEqual(bank.count_questions("t2", search="number 99"), 1)
        self.assertEqual(bank.subject_counts("t2")["Chemistry"], 8)
    def test_bulk_add_and_export_roundtrip(self):
        rows = [dict(Q(i), subject="Biology", topic="Genetics") for i in range(50, 55)] + [Q(50)] + [Q(60, answer="X")]
        r = bank.bulk_add("t1", rows, source="csv"); self.assertEqual((r["added"], r["duplicates"], len(r["invalid"])), (5, 1, 1))
        text = qt.to_csv(bank.export_rows("t1", "Biology")); good, errs = qt.parse_csv(text); self.assertGreaterEqual(len(good), 5); self.assertEqual(errs, [])
    def test_student_picks_unseen_first_and_includes_shared(self):
        db.ensure_student("stu2", "S2")
        for i in range(70, 76): bank.add_question("t1", Q(i), "Biology", "Cells", shared=(i >= 73))      # 73-75 shared
        for i in range(76, 78): bank.add_question("stu2", Q(i), "Biology", "Cells")
        self.assertEqual(bank.available_for_student("stu2", "Biology"), 3 + 2)                             # shared + own, not t1's private ones
        db.record_quiz("stu2", "Biology", "Cells", [Q(73), Q(74)], {0: "A"}, "Medium")                    # sees two of them
        got = bank.pick_for_student("stu2", "Biology", 3, seed=1)
        self.assertEqual(len(got), 3); self.assertTrue(all(g["owner_id"] in ("t1", "stu2") for g in got))
        self.assertFalse({g["question"] for g in got[:3]} & {Q(73)["question"], Q(74)["question"]})        # fresh first
        more = bank.pick_for_student("stu2", "Biology", 10, seed=1); self.assertEqual(len(more), 5)        # tops up with seen ones, never invents
        self.assertEqual(len(bank.pick_questions("t1", 4, subject="Biology", seed=3)), 4)

if __name__ == "__main__":
    unittest.main(verbosity=1)
