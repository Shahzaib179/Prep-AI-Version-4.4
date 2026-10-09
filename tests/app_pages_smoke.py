"""Headless run of app.py: every page is executed against a FAKE Streamlit (no browser, no API key).
Proves the new code is wired correctly (no NameError / bad arguments). It cannot judge how pages LOOK.
Run:  python tests/app_pages_smoke.py
"""
import contextlib, os, pathlib, sys, tempfile, types
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT, OVR = [], {}

class Rerun(Exception): pass
class Stop(Exception): pass
class State(dict):
    def __getattr__(self, k):
        try: return self[k]
        except KeyError: raise AttributeError(k)
    def __setattr__(self, k, v): self[k] = v

class Ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __getattr__(self, n): return getattr(FakeSt, n)

class FakeSt(types.ModuleType):
    session_state = State(); secrets = {}
    def __init__(self): super().__init__("streamlit")
    @staticmethod
    def _say(*a, **k): OUT.append(" ".join(str(x) for x in a if isinstance(x, (str, int, float))))
    markdown = write = caption = info = warning = error = success = subheader = title = code = _say
    set_page_config = divider = staticmethod(lambda *a, **k: None)
    @staticmethod
    def stop(): raise Stop()
    @staticmethod
    def rerun(*a, **k): raise Rerun()
    @staticmethod
    def exception(e): raise e
    @staticmethod
    def fragment(*a, **k): return (lambda f: f) if not (a and callable(a[0])) else a[0]
    @staticmethod
    def cache_resource(*a, **k): return (lambda f: f) if not (a and callable(a[0])) else a[0]
    sidebar = Ctx()
    spinner = expander = form = container = chat_message = staticmethod(lambda *a, **k: Ctx())
    @staticmethod
    def columns(n, **k): return [Ctx() for _ in range(n if isinstance(n, int) else len(n))]
    @staticmethod
    def tabs(names): return [Ctx() for _ in names]
    @staticmethod
    def metric(label, value, **k): OUT.append(f"METRIC {label}={value}")
    @staticmethod
    def progress(v, text=None, **k): OUT.append(f"PROGRESS {text}")
    @staticmethod
    def altair_chart(c, **k): OUT.append("CHART")
    @staticmethod
    def dataframe(d, **k): OUT.append("TABLE")
    @staticmethod
    def download_button(label, data, *a, **k): OUT.append(f"DOWNLOAD {label}")
    @staticmethod
    def radio(label, options=(), index=0, key=None, **k):
        opts = list(options); return OVR.get(key or label, opts[index] if (opts and index is not None) else None)
    @staticmethod
    def selectbox(label, options, index=0, key=None, **k): return OVR.get(key or label, list(options)[index or 0])
    @staticmethod
    def multiselect(label, options, key=None, default=None, **k): return OVR.get(key or label, default or [])
    @staticmethod
    def slider(label, min_value=0, max_value=100, value=None, key=None, **k): return OVR.get(key or label, value if value is not None else min_value)
    @staticmethod
    def number_input(label, min_value=None, max_value=None, value=0, *a, key=None, **k): return OVR.get(key or label, value)
    @staticmethod
    def date_input(label, value=None, key=None, **k): return OVR.get(key or label, value)
    @staticmethod
    def text_input(label, value="", key=None, **k): return OVR.get(key or label, value)
    text_area = text_input
    @staticmethod
    def color_picker(label, value="#000000", key=None, **k): return OVR.get(key or label, value)
    @staticmethod
    def checkbox(label, value=False, key=None, **k): return OVR.get(key or label, FakeSt.session_state.get(key, value) if key else value)
    @staticmethod
    def button(label, key=None, **k): return OVR.get(key or label, False)
    form_submit_button = button
    @staticmethod
    def time_input(label, value=None, key=None, **k): return OVR.get(key or label, value)
    popover = staticmethod(lambda *a, **k: Ctx())
    @staticmethod
    def link_button(label, url, **k): OUT.append(f"LINK {label}")
    @staticmethod
    def file_uploader(*a, **k): return None
    @staticmethod
    def audio_input(*a, **k): return None
    @staticmethod
    def chat_input(*a, **k): return None

fake = FakeSt(); sys.modules["streamlit"] = fake
for n in ("groq", "ddgs", "faiss", "sentence_transformers", "gdown", "gtts", "altair"):
    m = mock.MagicMock(name=n); sys.modules[n] = m
import config
tmp = pathlib.Path(tempfile.mkdtemp()); config.DB_PATH = tmp / "t.db"; config.MEMORY_DIR = tmp / "mem"; config.MEMORY_DIR.mkdir()
import memory as _m
_m.embed_texts = lambda texts: __import__("numpy").ones((len(texts), 4), dtype="float32")

SRC = (ROOT / "app.py").read_text()
QS_OPT = [{"question": f"OptQ{i}", "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "answer": "A", "concept": "c", "explanation": "e", "difficulty": "Medium"} for i in range(3)]
QS = [{"question": f"Q{i}", "options": {"A": "a", "B": "b"}, "answer": "A", "concept": "c", "explanation": "e", "difficulty": "Medium"} for i in range(3)]

def run(page, **state):
    """Execute app.py once as a signed-in user viewing `page`."""
    OUT.clear()
    ss = fake.session_state
    ss.update({"profile_complete": True, "student_id": "stu1", "student_name": "Ali", **state})
    OVR["Navigation"] = page
    g = {"__name__": "__main__", "__file__": str(ROOT / "app.py")}
    try: exec(compile(SRC, "app.py", "exec"), g)
    except (Rerun, Stop): pass
    return g

failures = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <- {detail}"))
    if not cond: failures.append(name)

import db
# ---- sign in as a new student, then visit every page
g = run("Dashboard")
check("dashboard renders (rings + donut charts)", OUT.count("CHART") >= 3, OUT[:5])
for p in ("Today", "Learn", "Practice", "Exam", "Mock Test", "My Classes", "Published Quizzes", "AI Tutor", "Merit Calculator", "Path Finder", "Study Plan", "Memory", "History", "Settings"):
    try:
        run(p); check(f"page '{p}' runs", True)
    except Exception as e:
        check(f"page '{p}' runs", False, repr(e))
run("Memory"); check("memory page shows usage meter", any("Memories stored" in o for o in OUT))
run("Settings"); check("settings has Appearance", any("Appearance" in o for o in OUT))

# ---- Today page: progress strip, goal, retake of past mistakes (no AI needed)
run("Today"); check("Today page shows streak/level/goal", any(o.startswith("METRIC 🔥 Streak") for o in OUT) and any(o.startswith("METRIC 🎯 Today") for o in OUT), OUT[:10])
run("Dashboard"); check("dashboard shows the progress strip", any(o.startswith("METRIC 🔥 Streak") for o in OUT))
db.ensure_student("stu9", "Retaker")
db.record_quiz("stu9", "Biology", "Cells", QS_OPT, {0: "B", 1: "A", 2: "C"}, "Medium")
ss0 = fake.session_state
OVR["retake_btn"] = True; run("Today", student_id="stu9", student_name="Retaker", quiz=None); OVR.clear()
check("retake quiz starts from past mistakes", ss0["quiz"] and ss0["quiz"].get("origin") == "retake" and len(ss0["quiz"]["questions"]) == 2, ss0.get("quiz"))
uidr = ss0["quiz"]["uid"]; OVR["start_" + uidr] = True; run("Today", student_id="stu9", student_name="Retaker"); OVR.clear()
ss0["quiz"]["answers"] = {0: "A", 1: "A"}; OVR["submit_" + uidr] = True; run("Today", student_id="stu9", student_name="Retaker"); OVR.clear()
check("retake result earns XP and fixes the mistakes", ss0["quiz"]["result"] and ss0["quiz"]["result"].get("xp", 0) > 0 and db.progress.retakeable_mistakes("stu9") == [], ss0["quiz"].get("result"))
ss0["quiz"] = None; ss0["student_id"] = "stu1"; ss0["student_name"] = "Ali"

# ---- tutor navigation + dashboard
run("Dashboard", is_tutor=False); 
check("non-tutor cannot open tutor page", not any("Tutor Dashboard" in o and "METRIC" in o for o in OUT))
db.ensure_student("stu2", "Sara")
db.record_quiz("stu2", "Biology", "Cells", QS, {0: "A", 1: "A", 2: "B"}, "Medium")
_c0 = db.create_shared_quiz("stu1", "Scoped", "Biology", "Cells", "Medium", QS, 0)       # stu1 acts as the tutor here
db.record_quiz("stu2", "Biology", "Cells", QS, {0: "A", 1: "A", 2: "B"}, "Medium", shared_code=_c0, time_taken_sec=20)
run("Tutor Dashboard", is_tutor=True)
check("tutor dashboard shows class table + charts", "TABLE" in OUT and "CHART" in OUT and any(o.startswith("METRIC Students") for o in OUT), OUT[:8])
run("Tutor Dashboard", is_tutor=False); check("tutor page refuses a non-tutor", any("Tutor access is required" in o for o in OUT), OUT[:5])

# ---- timed practice quiz: gate -> start -> auto-submit at expiry
import time
ss = fake.session_state
g = run("Practice", quiz=None)
quiz_mod = run("Practice")  # builds helpers in globals
new_quiz = quiz_mod["new_quiz"]
ss["quiz"] = new_quiz([dict(q) for q in QS], "Biology", "Cells", "Medium", [], 120)
run("Practice"); check("timed quiz waits for Start click", ss["quiz"]["started_at"] is None and any("Start Quiz" in o or "limit" in o for o in OUT), OUT[:6])
OVR["start_" + ss["quiz"]["uid"]] = True; run("Practice"); OVR.clear()
check("Start click begins the clock", ss["quiz"]["started_at"] is not None)
ss["quiz"]["answers"] = {0: "A", 1: "B"}; ss["quiz"]["answer_ts"] = {0: ss["quiz"]["started_at"] + 1, 1: ss["quiz"]["started_at"] + 2}
run("Practice"); check("quiz still open before the deadline", ss["quiz"]["result"] is None)
ss["quiz"]["started_at"] -= 500                                   # pretend 500s passed (limit 120s)
ss["quiz"]["answer_ts"] = {0: ss["quiz"]["started_at"] + 5, 1: ss["quiz"]["started_at"] + 400}   # Q2 picked AFTER the deadline
run("Practice")
res = ss["quiz"]["result"]
check("expired quiz is auto-submitted", res is not None and res["timed_out"] is True, res)
check("late answer is ignored, early one kept", res and res["correct"] == 1 and res["skipped"] == 2, res)
check("result saved in database as timed out", db.history("stu1")[0]["timed_out"] == 1 if db.history("stu1") else False)
run("Practice"); check("result screen shows score charts", OUT.count("CHART") >= 2, OUT[:6])
n_before = len(db.history("stu1")); run("Practice"); check("re-render never double-saves", len(db.history("stu1")) == n_before)

# ---- untimed quiz needs no Start click and manual submit works
ss["quiz"] = new_quiz([dict(q) for q in QS], "Biology", "Cells", "Medium", [], 0)
uid = ss["quiz"]["uid"]; run("Practice")
check("untimed quiz starts immediately", ss["quiz"]["started_at"] is not None)
ss["quiz"]["answers"] = {0: "A", 1: "A", 2: "A"}; OVR["submit_" + uid] = True; run("Practice"); OVR.clear()
check("manual submit scores 3/3", ss["quiz"]["result"] and ss["quiz"]["result"]["correct"] == 3 and not ss["quiz"]["result"]["timed_out"])

# ---- share + join
ss["quiz"] = new_quiz([dict(q) for q in QS], "Biology", "Cells", "Medium", [], 0)
uid = ss["quiz"]["uid"]; OVR["share_btn_" + uid] = True; run("Practice"); OVR.clear()
code = ss["quiz"].get("share_code_created")
check("share code created", bool(code) and len(code) == 6, code)
ss["student_id"] = "stu3"; ss["student_name"] = "Omar"; ss["shared_quiz"] = None
OVR["Quiz code"] = code; OVR["Open quiz"] = True; run("Published Quizzes", student_id="stu3", student_name="Omar"); OVR.clear()
check("student joins by code", ss["shared_quiz"] is not None and ss["shared_quiz"]["shared_code"] == code)
uid = ss["shared_quiz"]["uid"]; OVR["start_" + uid] = True; run("Published Quizzes", student_id="stu3", student_name="Omar"); OVR.clear()
ss["shared_quiz"]["answers"] = {0: "A", 1: "A", 2: "B"}; OVR["submit_" + uid] = True; run("Published Quizzes", student_id="stu3", student_name="Omar"); OVR.clear()
check("shared attempt saved under joiner's ID", [r["student_id"] for r in db.shared_quiz_results(code)] == ["stu3"])
ss["shared_quiz"] = None; OVR["Quiz code"] = code; OVR["Open quiz"] = True; run("Published Quizzes", student_id="stu3", student_name="Omar"); OVR.clear()
check("second attempt refused", ss["shared_quiz"] is None and any("already completed" in o for o in OUT), OUT[:4])

# ---- accounts: tutor/student sign-up and log-in screens
config.get_tutor_code = lambda: "secret-code"
def fresh_login_screen(**ovr):
    OVR.clear(); OVR.update(ovr); OUT.clear(); ss.update({"profile_complete": False, "student_id": "", "user_id": None, "_cookie_checked": False})
    g = {"__name__": "__main__", "__file__": str(ROOT / "app.py")}
    try: exec(compile(SRC, "app.py", "exec"), g)
    except (Rerun, Stop): pass
    OVR.clear()

tut = dict(tu_name="Dr Tutor", tu_email="tutor@example.com", tu_pw="Passw0rd!x", tu_pw2="Passw0rd!x")
fresh_login_screen(**tut, tu_invite="wrong", **{"Create my tutor account": True})
check("wrong tutor invite code refused", any("Incorrect tutor invite code" in o for o in OUT) and not ss["profile_complete"], OUT[:4])
fresh_login_screen(**{**tut, "tu_pw2": "different1"}, tu_invite="secret-code", **{"Create my tutor account": True})
check("mismatched passwords refused", any("do not match" in o for o in OUT) and not ss["profile_complete"], OUT[:4])
fresh_login_screen(**tut, tu_invite="secret-code", **{"Create my tutor account": True})
check("right invite code creates a tutor account and signs in", ss["profile_complete"] and ss["is_tutor"] and ss["student_id"].startswith("tutor_") and ss["user_id"], dict(ss))
TUTOR_ID = ss["student_id"]
fresh_login_screen(su_name="Ali Khan", su_email="ali@example.com", su_pw="Passw0rd!x", su_pw2="Passw0rd!x", su_level="MDCAT", **{"Create my student account": True})
check("student sign-up creates account and signs in", ss["profile_complete"] and not ss["is_tutor"] and ss["student_id"].startswith("stu_"), dict(ss))
STU_ID = ss["student_id"]
fresh_login_screen(login_email="ali@example.com", login_password="wrong-pass1", **{"Log in": True})
check("wrong password refused", any("Incorrect email or password" in o for o in OUT) and not ss["profile_complete"], OUT[:4])
fresh_login_screen(login_email="ALI@example.com", login_password="Passw0rd!x", **{"Log in": True})
check("log in restores the same student", ss["profile_complete"] and ss["student_id"] == STU_ID, dict(ss))
fresh_login_screen(login_email="tutor@example.com", login_password="Passw0rd!x", **{"Log in": True})
check("tutor logs in as tutor", ss["is_tutor"] and ss["student_id"] == TUTOR_ID, dict(ss))
OVR.clear()
navs = []
orig_radio = FakeSt.radio
FakeSt.radio = staticmethod(lambda label, options=(), index=0, key=None, **k: (navs.append(list(options)) if label == "Navigation" else None, OVR.get(key or label, list(options)[index] if options else None))[1])
run("Tutor Dashboard", is_tutor=True, student_id=TUTOR_ID, student_name="Tutor")
check("tutor navigation is only the Tutor Dashboard", navs and navs[0] == ["Tutor Dashboard"], navs)
navs.clear(); run("Dashboard", is_tutor=False, student_id="stu1", student_name="Ali")
check("student navigation has Published Quizzes, no Join Quiz/Tutor Dashboard", "Published Quizzes" in navs[0] and "Join Quiz" not in navs[0] and "Tutor Dashboard" not in navs[0], navs)
FakeSt.radio = staticmethod(orig_radio)

# ---- tutor publishes from the dashboard; students get their own question order
tid = TUTOR_ID
ss["quiz"] = new_quiz([dict(q, question=f"Q{i}") for i, q in enumerate(QS * 4)], "Biology", "Cells", "Medium", [], 0)
uid = ss["quiz"]["uid"]; OVR["share_btn_" + uid] = True; run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
tcode = ss["quiz"].get("share_code_created")
check("tutor publishes a quiz from the dashboard", bool(tcode) and [q["code"] for q in db.list_shared_quizzes(tid)] == [tcode], (tcode, tid, [q["code"] for q in db.list_shared_quizzes()]))
orders = []
for sid in ("a1", "b2", "c3", "d4"):
    ss["shared_quiz"] = None; OVR["Quiz code"] = tcode; OVR["Open quiz"] = True
    run("Published Quizzes", student_id=sid, student_name=sid, is_tutor=False); OVR.clear()
    orders.append(tuple(q["question"] for q in ss["shared_quiz"]["questions"]))
check("same questions for everyone", len({tuple(sorted(o)) for o in orders}) == 1)
check("each student gets a different order", len(set(orders)) > 1, orders)
ss["shared_quiz"] = None
run("Published Quizzes", student_id="a1", student_name="a1", is_tutor=False)
check("students see results as locked", any("visible only to the tutor" in o for o in OUT), OUT[:6])

# ---- question bank, editor and option shuffling (tutor flow)
import bank, question_tools as _qt
for i, q in enumerate(QS_OPT * 3):                                    # 9 distinct 4-option questions
    bank.add_question(tid, dict(q, question=f"Bank question number {i} about cells?"), "Biology", "Cells")
OVR["bq_go"] = True; OVR["bq_n"] = 6; OVR["bq_subject"] = "Biology"
ss["quiz"] = None; run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
check("tutor builds a quiz from the bank", ss["quiz"] and len(ss["quiz"]["questions"]) == 6, ss.get("quiz"))
quid = ss["quiz"]["uid"]
OVR[f"ed_{quid}_0_0_q"] = "EDITED: which organelle makes ATP in the cell?"; OVR[f"ed_{quid}_0_0_ans"] = "C"; OVR[f"ed_{quid}_0_1_del"] = True
OVR["Apply edits"] = True; run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
check("editor applies text/answer edits and removes a question", len(ss["quiz"]["questions"]) == 5 and ss["quiz"]["questions"][0]["question"].startswith("EDITED") and ss["quiz"]["questions"][0]["answer"] == "C", [q["question"][:20] for q in ss["quiz"]["questions"]])
OVR[f"ed_{quid}_1_0_ans"] = "Z"
# an invalid edit must not be applied
n_before = len(ss["quiz"]["questions"]); OVR[f"ed_{quid}_1_1_A"] = ""; OVR["Apply edits"] = True
run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
check("invalid edit is refused and the old question kept", len(ss["quiz"]["questions"]) == n_before and all(not _qt.validate_question(q) for q in ss["quiz"]["questions"]), OUT[-4:])
quid = ss["quiz"]["uid"]; OVR["share_btn_" + quid] = True; run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
ocode = ss["quiz"].get("share_code_created"); check("edited quiz published", bool(ocode))
letters_of_correct = []
for sid in ("o1", "o2", "o3", "o4", "o5", "o6"):
    ss["shared_quiz"] = None; OVR["Quiz code"] = ocode; OVR["Open quiz"] = True
    run("Published Quizzes", student_id=sid, student_name=sid, is_tutor=False); OVR.clear()
    qz = ss["shared_quiz"]; texts = {q["question"]: q["options"][q["answer"]] for q in qz["questions"]}
    orig = {q["question"]: q["options"][q["answer"]] for q in db.get_shared_quiz(ocode)["questions"]}
    if sid == "o1": check("shuffle keeps the correct TEXT correct for every question", texts == orig, (texts, orig))
    letters_of_correct.append(tuple(q["answer"] for q in sorted(qz["questions"], key=lambda q: q["question"])))
    OVR["start_" + qz["uid"]] = True; run("Published Quizzes", student_id=sid, student_name=sid, is_tutor=False); OVR.clear()
    qz["answers"] = {i: q["answer"] for i, q in enumerate(qz["questions"])}; OVR["submit_" + qz["uid"]] = True
    run("Published Quizzes", student_id=sid, student_name=sid, is_tutor=False); OVR.clear()
    if sid == "o1": check("a student answering by the shuffled letters scores 100%", qz["result"] and qz["result"]["pct"] == 100.0, qz.get("result"))
check("different students see different option orders", len(set(letters_of_correct)) > 1, letters_of_correct)

# ---- mock test mode
import mock as _mock, classes as _cls
from datetime import date as _d, time as _t, timedelta as _td
for subj in ("Biology", "Chemistry"):
    for i in range(6):
        bank.add_question("mk1", dict(QS_OPT[0], question=f"Mock {subj} question number {i} here?"), subj, "T")
run("Mock Test", student_id="mk1", student_name="Mk"); check("Mock Test page runs", any("Mock Test" in o for o in OUT), OUT[:4])
OVR.update({"mk_type": "Custom", "mk_c_Biology": 4, "mk_c_Chemistry": 3, "mk_c_Physics": 0, "mk_c_English": 0, "mk_c_Logical Reasoning": 0, "mk_ai": False, "mk_build": True})
ss["mock"] = None; run("Mock Test", student_id="mk1", student_name="Mk"); OVR.clear()
mq = ss.get("mock"); check("mock test is built from the bank", mq and len(mq["questions"]) == 7, mq and len(mq["questions"]))
check("mock progress saved for resume", _mock.load_progress("mk1") is not None)
OVR["mk_start_" + mq["uid"]] = True; run("Mock Test", student_id="mk1", student_name="Mk"); OVR.clear()
check("mock clock starts on click", mq["started_at"] is not None)
mq["answers"] = {i: q["answer"] for i, q in enumerate(mq["questions"][:5])}; mq["answer_ts"] = {i: mq["started_at"] + 1 for i in mq["answers"]}
OVR["mk_submit_" + mq["uid"]] = True; run("Mock Test", student_id="mk1", student_name="Mk"); OVR.clear()
check("mock result has section scores and is saved", mq["result"] and mq.get("scores") and len(_mock.history("mk1")) == 1 and _mock.load_progress("mk1") is None, mq.get("result"))
ss["mock"] = None

# ---- classes with deadlines
cl = _cls.create_class(tid, "Morning batch")
run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); check("tutor Classes tab renders", any("Classes and deadlines" in o for o in OUT), OUT[:5])
ok, _m_, _c = _cls.join_class("cs1", cl["join_code"]); check("student joins class by code", ok)
dcode = db.create_shared_quiz(tid, "Due quiz", "Biology", "Cells", "Medium", QS, 0)
soon = _cls.local_to_utc_iso(_d.today() + _td(days=2), _t(12, 0))
check("assignment with future deadline created", _cls.create_assignment(tid, cl["id"], dcode, "", soon, False)[0])
ss["shared_quiz"] = None
try: run("My Classes", student_id="cs1", student_name="Cs", is_tutor=False)
except Exception as e: import traceback; traceback.print_exc()
check("My Classes lists the assignment", any("Due quiz" in o or "due" in o for o in OUT), OUT[6:])
ss["shared_quiz"] = None; OVR["Quiz code"] = dcode; OVR["Open quiz"] = True; run("Published Quizzes", student_id="cs1", student_name="Cs", is_tutor=False); OVR.clear()
check("student can open the assigned quiz before the deadline", ss["shared_quiz"] is not None)
import sqlite3
with db.connect() as _con: _con.execute("UPDATE assignments SET due_at=? WHERE quiz_code=?", ("2020-01-01T00:00", dcode))
ss["shared_quiz"] = None; OVR["Quiz code"] = dcode; OVR["Open quiz"] = True; run("Published Quizzes", student_id="cs1", student_name="Cs", is_tutor=False); OVR.clear()
check("late start is blocked after the deadline", ss["shared_quiz"] is None and any("deadline" in o for o in OUT), OUT[:4])
ss["shared_quiz"] = None; OVR["Quiz code"] = dcode; OVR["Open quiz"] = True; run("Published Quizzes", student_id="outsider", student_name="Out", is_tutor=False); OVR.clear()
check("students outside the class are not bound by the deadline", ss["shared_quiz"] is not None)
ss["shared_quiz"] = None
with db.connect() as _con: _con.execute("UPDATE assignments SET allow_late=1 WHERE quiz_code=?", (dcode,))
OVR["Quiz code"] = dcode; OVR["Open quiz"] = True; run("Published Quizzes", student_id="cs1", student_name="Cs", is_tutor=False); OVR.clear()
check("allow-late lets the student start (with a warning)", ss["shared_quiz"] is not None and any("late" in o for o in OUT), OUT[:4])
ss["shared_quiz"] = None

# ---- Month 2: daily challenge, live quiz, reminders, AI quality tab
import daily as _daily, live as _live
OVR.clear(); ss["daily_quiz"] = None
run("Daily Challenge", student_id="dc1", student_name="Dee", is_tutor=False); check("Daily Challenge page runs", any("Daily Challenge" in o for o in OUT), OUT[:3])
OVR["daily_start"] = True; run("Daily Challenge", student_id="dc1", student_name="Dee", is_tutor=False); OVR.clear()
dq = ss.get("daily_quiz"); check("daily challenge builds 5 questions offline", dq and len(dq["questions"]) == 5 and dq["time_limit_sec"] == 300, dq and len(dq["questions"]))
OVR["start_" + dq["uid"]] = True; run("Daily Challenge", student_id="dc1", student_name="Dee", is_tutor=False); OVR.clear()
dq["answers"] = {i: q["answer"] for i, q in enumerate(dq["questions"])}; dq["answer_ts"] = {i: dq["started_at"] + 1 for i in dq["answers"]}
OVR["submit_" + dq["uid"]] = True; run("Daily Challenge", student_id="dc1", student_name="Dee", is_tutor=False); OVR.clear()
run("Daily Challenge", student_id="dc1", student_name="Dee", is_tutor=False)   # the app re-runs once after submit and stores the day's result then
res_d = _daily.result_for("dc1", _daily.today())
check("daily result stored with bonus XP", res_d and res_d["correct"] == 5 and res_d["xp"] == _daily.BONUS_XP + _daily.BONUS_PERFECT, res_d)
run("Daily Challenge", student_id="dc1", student_name="Dee", is_tutor=False); check("page shows done + leaderboard after finishing", any("already done" in o or "challenge is done" in o for o in OUT) and "TABLE" in OUT, OUT[:6])
ss["daily_quiz"] = None
OVR["daily_start"] = True; run("Daily Challenge", student_id="dc1", student_name="Dee", is_tutor=False); OVR.clear()
check("second attempt on the same day is not offered", ss.get("daily_quiz") is None)

# live quiz: tutor creates from the bank, student joins and answers
for i in range(3): bank.add_question(tid, dict(QS_OPT[0], question=f"Live bank question {i} about the cell?"), "Biology", "Cells")
OVR.update({"lv_create": True, "lv_subject": "Biology", "lv_n": 3}); ss["live_host_code"] = None
run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
lcode = ss.get("live_host_code"); check("tutor creates a live quiz", bool(lcode) and len(lcode) == 6, lcode)
run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); check("host console shows the join code", any(lcode in o for o in OUT), OUT[:5])
ss["live_code"] = None; OVR["Live quiz code"] = lcode; OVR["Join"] = True
run("Live Quiz", student_id="lp1", student_name="Pat", is_tutor=False); OVR.clear()
check("student joins the live quiz", ss.get("live_code") == lcode)
run("Live Quiz", student_id="lp1", student_name="Pat", is_tutor=False); check("student waits in the lobby", any("Waiting for the tutor" in o for o in OUT), OUT[:5])
OVR["lv_start"] = True; run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
check("host starts the game", _live.get_state(lcode)["state"] == _live.QUESTION)
run("Live Quiz", student_id="lp1", student_name="Pat", is_tutor=False); check("student sees the question", any("Question 1 of 3" in o for o in OUT), OUT[:6])
sid_l = _live.get_state(lcode)["id"]; q0 = _live.get_questions(sid_l)[0]
shown = _live.display_question(q0, "lp1", sid_l); pressed = next(k for k, v in shown["options"].items() if v == q0["options"][q0["answer"]])
OVR[f"lvp_{sid_l}_0_{pressed}"] = True; run("Live Quiz", student_id="lp1", student_name="Pat", is_tutor=False); OVR.clear()
check("correct answer by shuffled letter scores points", (_live.my_answer(sid_l, "lp1", 0) or {}).get("is_correct") == 1, _live.my_answer(sid_l, "lp1", 0))
run("Live Quiz", student_id="lp1", student_name="Pat", is_tutor=False); check("student sees answer locked", any("locked" in o for o in OUT), OUT[:6])
_live.end_session(lcode, tid); run("Live Quiz", student_id="lp1", student_name="Pat", is_tutor=False)
check("finished game shows the result and credits progress", any("over" in o for o in OUT) and len(db.history("lp1")) == 1, OUT[:5])
ss["live_code"] = None; ss["live_host_code"] = None

# reminders + AI quality
OVR["rem_preview"] = True; run("Today", student_id="dc1", student_name="Dee", is_tutor=False); OVR.clear()
check("reminder preview works", any(o.startswith("LINK") for o in OUT) or any("caught up" in o for o in OUT), OUT[-4:])
run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); check("AI Quality tab renders", any("AI quality" in o for o in OUT), OUT[:3])
OVR["aiq_import"] = True; run("Tutor Dashboard", is_tutor=True, student_id=tid, student_name="Tutor"); OVR.clear()
check("starter pack imported into the bank", bank.count_questions(tid, subject="Chemistry") >= 10, bank.count_questions(tid))

# ---- memory reset button
from memory import LongTermMemory
ss["student_id"] = "stu1"; ss["student_name"] = "Ali"
for i in range(3): LongTermMemory("stu1").add(f"memory item {i} about cells", "learning")
check("memory exists before reset", LongTermMemory("stu1").count() == 3)
# the button callback is passed to st.button(on_click=...): capture and invoke it
captured = {}
orig_button = FakeSt.button
FakeSt.button = staticmethod(lambda label, key=None, **k: captured.setdefault(label, k.get("on_click")) and False)
ss["reset_confirm"] = True; ss["reset_incl_chats"] = False
run("Memory"); FakeSt.button = orig_button
cb = captured.get("Reset my memory now"); check("reset button wired with callback", callable(cb))
cb(); check("callback empties memory", LongTermMemory("stu1").count() == 0)
check("confirmation box un-ticks itself", ss["reset_confirm"] is False)
run("Memory"); check("page tells the user what was removed", any("Memory reset" in o for o in OUT), OUT[:5])

# ---- theme applied
db.update_preferences("stu1", theme_preset="Dark")
for k in ("theme_preset", "theme_bg", "theme_text"): ss.pop(k, None)
run("Dashboard"); check("saved Dark theme loads and reaches charts", ss["theme_pair"] == ("#0E1117", "#F3F4F6"), ss.get("theme_pair"))

print("\nFAILED:" if failures else "\nALL PASSED", failures or "")
sys.exit(1 if failures else 0)
