"""Classes, enrolment, assignments and deadline rules."""
import pathlib, sys, tempfile, types, unittest
from datetime import date, datetime, time, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
class _St(types.ModuleType):
    session_state = {}; secrets = {}
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
sys.modules["streamlit"] = _St("streamlit")
import config; config.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "c.db"
import db, classes as cl  # noqa: E402
db.init_db()
QS = [{"question": f"Q{i} about cells?", "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "answer": "A", "concept": "c", "explanation": "e"} for i in range(3)]
N = datetime.utcnow()
def iso(delta): return (N + delta).isoformat(timespec="minutes")
def mk(sid, role="student"):
    db.ensure_student(sid, sid)
    if role == "tutor": db.set_role(sid, "tutor")

class T(unittest.TestCase):
    def setUp(self):
        for s in ("tutA", "tutB"): mk(s, "tutor")
        for s in ("s1", "s2", "s3"): mk(s)

    def test_time_conversion_round_trip(self):
        u = cl.local_to_utc_iso(date(2026, 10, 20), time(23, 30))               # Pakistan UTC+5
        self.assertEqual(u, "2026-10-20T18:30"); self.assertEqual(cl.utc_iso_to_local(u).strftime("%H:%M"), "23:30")
        self.assertEqual(cl.fmt_local(None), "No deadline")

    def test_class_lifecycle_and_codes(self):
        c = cl.create_class("tutA", "  Biology   MDCAT 2026 "); self.assertEqual(c["name"], "Biology MDCAT 2026"); self.assertEqual(len(c["join_code"]), 6)
        with self.assertRaises(ValueError): cl.create_class("tutA", "x")
        ok, msg, _ = cl.join_class("s1", c["join_code"].lower()); self.assertTrue(ok)
        self.assertTrue(cl.join_class("s1", c["join_code"])[0]); self.assertIn("already", cl.join_class("s1", c["join_code"])[1])
        self.assertFalse(cl.join_class("s1", "ZZZZZZ")[0]); self.assertFalse(cl.join_class("tutB", c["join_code"])[0]); self.assertFalse(cl.join_class("tutA", c["join_code"])[0])
        self.assertEqual([m["student_id"] for m in cl.members("tutA", c["id"])], ["s1"]); self.assertEqual(cl.members("tutB", c["id"]), [])      # other tutor sees nothing
        self.assertEqual(cl.list_classes("tutA")[0]["members"], 1); self.assertNotIn("Biology MDCAT 2026", [x["name"] for x in cl.list_classes("tutB")])
        self.assertFalse(cl.remove_member("tutB", c["id"], "s1")); self.assertTrue(cl.remove_member("tutA", c["id"], "s1"))
        cl.join_class("s2", c["join_code"]); cl.set_class_active("tutA", c["id"], False); self.assertFalse(cl.join_class("s3", c["join_code"])[0])
        self.assertFalse(cl.set_class_active("tutB", c["id"], True))
        self.assertTrue(cl.leave_class("s2", c["id"]))

    def test_assignment_rules(self):
        c = cl.create_class("tutA", "Chem class"); other = cl.create_class("tutB", "Other")
        code = db.create_shared_quiz("tutA", "Atoms quiz", "Chemistry", "Atoms", "Medium", QS)
        codeB = db.create_shared_quiz("tutB", "B quiz", "Chemistry", "Atoms", "Medium", QS)
        self.assertFalse(cl.create_assignment("tutA", c["id"], codeB, "x", iso(timedelta(days=1)))[0])          # not their quiz
        self.assertFalse(cl.create_assignment("tutB", c["id"], code, "x", iso(timedelta(days=1)))[0])           # not their class
        self.assertFalse(cl.create_assignment("tutA", c["id"], code, "x", iso(-timedelta(hours=1)))[0])         # deadline in the past
        self.assertTrue(cl.create_assignment("tutA", c["id"], code, "", iso(timedelta(days=1)))[0])
        self.assertIn("already", cl.create_assignment("tutA", c["id"], code, "x", iso(timedelta(days=2)))[1])
        self.assertEqual(cl.list_assignments("tutA", c["id"])[0]["title"], "Atoms quiz")                         # title defaults to the quiz title
        self.assertEqual(cl.list_assignments("tutB", c["id"]), [])

    def test_status_matrix(self):
        due = N + timedelta(hours=10); f = cl.status_for
        att_ok = {"created_at": iso(timedelta(hours=-1)), "started_at": iso(timedelta(hours=-2))}
        self.assertEqual(f(None, False, None, N), "open"); self.assertEqual(f(due.isoformat(), False, None, N), "due_soon")
        self.assertEqual(f((N + timedelta(days=5)).isoformat(), False, None, N), "open")
        self.assertEqual(f((N - timedelta(hours=1)).isoformat(), False, None, N), "missed"); self.assertEqual(f((N - timedelta(hours=1)).isoformat(), True, None, N), "overdue")
        self.assertEqual(f(due.isoformat(), False, att_ok, N), "done")
        late = {"created_at": iso(timedelta(hours=-1)), "started_at": iso(timedelta(hours=-2))}
        self.assertEqual(f((N - timedelta(hours=5)).isoformat(), True, late, N), "done_late")
        started_in_time = {"created_at": iso(timedelta(hours=1)), "started_at": iso(timedelta(hours=-1))}        # finished after the deadline but started before it
        self.assertEqual(f(N.isoformat(), False, started_in_time, N + timedelta(hours=2)), "done")

    def test_deadline_enforcement_and_reports(self):
        c = cl.create_class("tutA", "Physics class"); cl.join_class("s1", c["join_code"]); cl.join_class("s2", c["join_code"]); cl.join_class("s3", c["join_code"])
        code = db.create_shared_quiz("tutA", "Waves", "Physics", "Waves", "Medium", QS)
        self.assertIsNone(cl.deadline_state("s1", code))                                                         # not assigned yet: free to take
        cl.create_assignment("tutA", c["id"], code, "Waves quiz", iso(timedelta(hours=3)), allow_late=False)
        st = cl.deadline_state("s1", code); self.assertFalse(st["blocked"])
        self.assertTrue(cl.deadline_state("s1", code, N + timedelta(hours=4))["blocked"])
        self.assertFalse(cl.deadline_state("tutB", code, N + timedelta(hours=4)) and True)                        # outsiders are unaffected
        db.shared_quiz_start(code, "s1"); db.record_quiz("s1", "Physics", "Waves", QS, {0: "A", 1: "A", 2: "B"}, "Medium", shared_code=code, time_taken_sec=60)
        db.record_quiz("s2", "Physics", "Waves", QS, {0: "A"}, "Medium", shared_code=code, time_taken_sec=90)
        rep = cl.assignment_report("tutA", cl.list_assignments("tutA", c["id"])[0]["id"])
        self.assertEqual((rep["members"], rep["completed"], rep["late"]), (3, 2, 0)); self.assertEqual([m["student_id"] for m in rep["missing"]], ["s3"])
        self.assertEqual(rep["average"], round((66.7 + 33.3) / 2, 1)); self.assertIsNone(cl.assignment_report("tutB", rep["id"]))
        mine = cl.student_assignments("s3"); self.assertEqual((len(mine), mine[0]["status"]), (1, "due_soon"))
        self.assertEqual(cl.student_assignments("s1")[0]["status"], "done"); self.assertEqual(cl.pending_count("s1"), 0); self.assertEqual(cl.pending_count("s3"), 1)
        self.assertEqual(cl.student_assignments("tutB"), [])
        cl.update_deadline("tutA", rep["id"], iso(timedelta(days=3)), True); self.assertTrue(cl.deadline_state("s3", code, N + timedelta(days=4))["late"])
        self.assertFalse(cl.deadline_state("s3", code, N + timedelta(days=4))["blocked"])                        # late allowed
        self.assertTrue(cl.delete_assignment("tutA", rep["id"])); self.assertFalse(cl.delete_assignment("tutA", rep["id"]))

    def test_roster_scope_includes_class_members_without_attempts(self):
        c = cl.create_class("tutB", "Scope"); cl.join_class("s3", c["join_code"])
        self.assertIn("s3", [r["student_id"] for r in db.roster("tutB")])
        mk("loner"); self.assertNotIn("loner", [r["student_id"] for r in db.roster("tutB")])

if __name__ == "__main__":
    unittest.main(verbosity=1)
