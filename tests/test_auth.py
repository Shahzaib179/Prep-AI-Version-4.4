"""Accounts: hashing, sign-up, login, lockout, tutor invite, legacy claim, sessions, change password."""
import pathlib, sys, tempfile, types, unittest
from datetime import datetime, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
class _St(types.ModuleType):
    session_state = {}; secrets = {}
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
sys.modules["streamlit"] = _St("streamlit")
TMP = pathlib.Path(tempfile.mkdtemp())
import config; config.DB_PATH = TMP / "auth.db"
import secrets as _secrets
import auth, db, db_core  # noqa: E402
from auth import AuthError
if db_core.is_postgres() and __import__("os").environ.get("CONTRACT_ALLOW_RESET") != "yes":
    sys.exit("Refusing to run against Postgres without CONTRACT_ALLOW_RESET=yes (use a STAGING database).")
db.init_db()
RUN = _secrets.token_hex(3)                      # unique per run so the test can be repeated on a shared staging database
CODE = f"tutor-secret-code-{RUN}"
SUFFIX = "@prepai-test.invalid"
n = [0]

def email():
    n[0] += 1; return f"{RUN}.user{n[0]}{SUFFIX}"

def tearDownModule():
    """Remove every row this file created (matters on a shared staging database)."""
    with db_core.connect() as con:
        sids = [r[0] for r in con.execute("SELECT student_id FROM users WHERE email LIKE ?", (f"%{SUFFIX}",)).fetchall()] + [f"OLD-{RUN}", auth.legacy_tutor_id(CODE)]
        for sid in sids:
            for t in ("question_attempts", "mistakes", "mastery", "revision_schedule", "quiz_attempts", "student_preferences"):
                con.execute(f"DELETE FROM {t} WHERE student_id=?", (sid,))
            con.execute("DELETE FROM shared_quizzes WHERE created_by=?", (sid,))
            con.execute("DELETE FROM students WHERE id=?", (sid,))
        con.execute("DELETE FROM auth_sessions WHERE user_id IN (SELECT id FROM users WHERE email LIKE ?)", (f"%{SUFFIX}",))
        con.execute("DELETE FROM users WHERE email LIKE ?", (f"%{SUFFIX}",))

class Passwords(unittest.TestCase):
    def test_hash_is_salted_verifiable_and_not_plaintext(self):
        a, b = auth.hash_password("Secret123"), auth.hash_password("Secret123")
        self.assertNotEqual(a, b); self.assertNotIn("Secret123", a)
        self.assertTrue(auth.verify_password("Secret123", a)); self.assertFalse(auth.verify_password("secret123", a))
        self.assertFalse(auth.verify_password("x", "garbage")); self.assertFalse(auth.needs_rehash(a))
    def test_policy(self):
        for bad in ("short1", "12345678", "abcdefgh", "password1", "A" * 200):
            with self.assertRaises(AuthError): auth.validate_password(bad)
        with self.assertRaises(AuthError): auth.validate_password("me@x.com", "me@x.com")
        auth.validate_password("goodpass9")
    def test_email(self):
        self.assertEqual(auth.validate_email("  Ali@Example.COM "), "ali@example.com")
        for bad in ("", "nope", "a@b", "a b@c.com"):
            with self.assertRaises(AuthError): auth.validate_email(bad)

class Accounts(unittest.TestCase):
    def test_student_signup_login_and_data_link(self):
        e = email(); u = auth.register_student(e, "Passw0rd!x", "Ali Khan", "MDCAT")
        self.assertEqual(u["role"], "student"); self.assertTrue(u["student_id"].startswith("stu_"))
        self.assertEqual(db.get_student(u["student_id"])["name"], "Ali Khan")
        self.assertEqual(auth.login(e.upper(), "Passw0rd!x")["student_id"], u["student_id"])     # email case-insensitive
        with self.assertRaises(AuthError): auth.register_student(e, "Passw0rd!x", "Dup", "MDCAT")   # no duplicate email

    def test_wrong_password_and_unknown_email_look_identical(self):
        e = email(); auth.register_student(e, "Passw0rd!x", "Sara Ali")
        msgs = set()
        for addr, pw in ((e, "wrongpass1"), ("ghost@prepai-test.invalid", "wrongpass1")):
            with self.assertRaises(AuthError) as cm: auth.login(addr, pw)
            msgs.add(str(cm.exception))
        self.assertEqual(msgs, {"Incorrect email or password."})

    def test_lockout_after_five_failures_then_unlock(self):
        e = email(); auth.register_student(e, "Passw0rd!x", "Omar Ali")
        for _ in range(4):
            with self.assertRaises(AuthError): auth.login(e, "bad-pass-1")
        with self.assertRaises(AuthError) as cm: auth.login(e, "bad-pass-1")                      # 5th failure locks
        self.assertIn("locked", str(cm.exception))
        with self.assertRaises(AuthError) as cm: auth.login(e, "Passw0rd!x")                      # even the RIGHT password is refused
        self.assertIn("locked", str(cm.exception))
        with db_core.connect() as con: con.execute("UPDATE users SET locked_until=? WHERE email=?", ((datetime.utcnow() - timedelta(minutes=1)).isoformat(), e))
        self.assertEqual(auth.login(e, "Passw0rd!x")["email"], e)                                  # lock expired
        with db_core.connect() as con: self.assertEqual(con.execute("SELECT failed_attempts FROM users WHERE email=?", (e,)).fetchone()[0], 0)

    def test_success_resets_failure_counter(self):
        e = email(); auth.register_student(e, "Passw0rd!x", "Hina Ali")
        for _ in range(3):
            with self.assertRaises(AuthError): auth.login(e, "bad-pass-1")
        auth.login(e, "Passw0rd!x")
        for _ in range(4):
            with self.assertRaises(AuthError): auth.login(e, "bad-pass-1")
        self.assertTrue(auth.login(e, "Passw0rd!x"))                                              # 4 fresh failures: not locked

    def test_legacy_student_id_can_be_claimed_once(self):
        db.ensure_student(f"OLD-{RUN}", "Old Student"); db.record_quiz(f"OLD-{RUN}", "Biology", "Cells", [{"question": "q", "options": {"A": "a"}, "answer": "A", "concept": "c"}], {0: "A"}, "Medium")
        u = auth.register_student(email(), "Passw0rd!x", "Old Student", "MDCAT", legacy_student_id=f"OLD-{RUN}")
        self.assertEqual(u["student_id"], f"OLD-{RUN}"); self.assertEqual(len(db.history(f"OLD-{RUN}")), 1)   # history kept
        with self.assertRaises(AuthError): auth.register_student(email(), "Passw0rd!x", "Thief", "MDCAT", legacy_student_id=f"OLD-{RUN}")
        with self.assertRaises(AuthError): auth.register_student(email(), "Passw0rd!x", "X Y", "MDCAT", legacy_student_id="NOPE-999")
        with self.assertRaises(AuthError): auth.register_student(email(), "Passw0rd!x", "X Y", "MDCAT", legacy_student_id="tutor_abc")

    def test_tutor_needs_the_invite_code(self):
        with self.assertRaises(AuthError): auth.register_tutor(email(), "Passw0rd!x", "Dr Tutor", "wrong", CODE)
        with self.assertRaises(AuthError): auth.register_tutor(email(), "Passw0rd!x", "Dr Tutor", CODE, "")           # disabled when no code configured
        u = auth.register_tutor(email(), "Passw0rd!x", "Dr Tutor", CODE, CODE)
        self.assertEqual(u["role"], "tutor"); self.assertEqual(db.get_role(u["student_id"]), "tutor")
        self.assertNotIn(u["student_id"], [r["student_id"] for r in db.roster()])

    def test_invite_code_brute_force_is_throttled(self):
        for _ in range(auth.INVITE_MAX_FAILS):
            with self.assertRaises(AuthError): auth.register_tutor(email(), "Passw0rd!x", "Dr Guess", "guess", CODE)
        with self.assertRaises(AuthError) as cm: auth.register_tutor(email(), "Passw0rd!x", "Dr Real", CODE, CODE)   # correct code now refused
        self.assertIn("Too many", str(cm.exception))
        with db_core.connect() as con:
            db._meta_set(con, "invite_locked_until", (datetime.utcnow() - timedelta(minutes=1)).isoformat())
        self.assertTrue(auth.register_tutor(email(), "Passw0rd!x", "Dr Real", CODE, CODE))

    def test_old_shared_quizzes_move_to_the_first_tutor_account(self):
        old = auth.legacy_tutor_id(CODE); db.ensure_student(old, "Tutor"); db.set_role(old, "tutor")
        qs = [{"question": "q", "options": {"A": "a"}, "answer": "A", "concept": "c"}]
        code = db.create_shared_quiz(old, "Legacy", "Biology", "Cells", "Medium", qs)
        u = auth.register_tutor(email(), "Passw0rd!x", "Dr New", CODE, CODE)
        self.assertEqual(db.get_shared_quiz(code)["created_by"], u["student_id"])

    def test_change_password_and_admin_reset(self):
        e = email(); u = auth.register_student(e, "Passw0rd!x", "Zara Ali"); tok = auth.create_session(u["id"])
        with self.assertRaises(AuthError): auth.change_password(u["id"], "wrong", "NewPassw0rd")
        with self.assertRaises(AuthError): auth.change_password(u["id"], "Passw0rd!x", "short")
        auth.change_password(u["id"], "Passw0rd!x", "NewPassw0rd")
        self.assertIsNone(auth.user_from_token(tok))                                               # other devices signed out
        with self.assertRaises(AuthError): auth.login(e, "Passw0rd!x")
        auth.login(e, "NewPassw0rd")
        self.assertTrue(auth.set_password_admin(e, "AdminSet123")); auth.login(e, "AdminSet123")
        self.assertFalse(auth.set_password_admin("nobody@prepai-test.invalid", "AdminSet123"))

class TutorIsolation(unittest.TestCase):
    QS = [{"question": "q", "options": {"A": "a", "B": "b"}, "answer": "A", "concept": "c"}]

    def test_each_tutor_sees_only_their_own_quizzes_and_students(self):
        ta = auth.register_tutor(email(), "Passw0rd!x", "Tutor A", CODE, CODE)["student_id"]
        tb = auth.register_tutor(email(), "Passw0rd!x", "Tutor B", CODE, CODE)["student_id"]
        sa = auth.register_student(email(), "Passw0rd!x", "Student Of A")["student_id"]
        sb = auth.register_student(email(), "Passw0rd!x", "Student Of B")["student_id"]
        qa = db.create_shared_quiz(ta, "A quiz", "Biology", "Cells", "Medium", self.QS)
        qb = db.create_shared_quiz(tb, "B quiz", "Biology", "Cells", "Medium", self.QS)
        db.record_quiz(sa, "Biology", "Cells", self.QS, {0: "A"}, "Medium", shared_code=qa, time_taken_sec=5)
        db.record_quiz(sb, "Biology", "Cells", self.QS, {0: "B"}, "Medium", shared_code=qb, time_taken_sec=5)
        self.assertEqual([r["student_id"] for r in db.roster(ta)], [sa])
        self.assertEqual([r["student_id"] for r in db.roster(tb)], [sb])
        self.assertEqual([q["code"] for q in db.list_shared_quizzes(ta)], [qa])
        self.assertFalse(db.set_shared_quiz_open(qb, False, owner_id=ta))                 # A cannot close B's quiz
        self.assertEqual(db.get_shared_quiz(qb)["is_open"], 1)
        self.assertTrue(db.set_shared_quiz_open(qb, False, owner_id=tb))
        self.assertEqual(db.roster(), db.roster(None))                                     # unscoped view still works for admin tools
        self.assertGreaterEqual(len(db.roster()), 2)


class Sessions(unittest.TestCase):
    def test_token_roundtrip_expiry_revoke_and_hashed_storage(self):
        u = auth.register_student(email(), "Passw0rd!x", "Tok Ali"); tok = auth.create_session(u["id"])
        self.assertEqual(auth.user_from_token(tok)["student_id"], u["student_id"])
        with db_core.connect() as con:
            stored = [r[0] for r in con.execute("SELECT token_hash FROM auth_sessions").fetchall()]
        self.assertNotIn(tok, stored)                                                              # only a hash is stored
        self.assertIsNone(auth.user_from_token("x" * 40)); self.assertIsNone(auth.user_from_token(""))
        expired = auth.create_session(u["id"], days=-1); self.assertIsNone(auth.user_from_token(expired))
        auth.revoke_session(tok); self.assertIsNone(auth.user_from_token(tok))

if __name__ == "__main__":
    unittest.main(verbosity=1)
