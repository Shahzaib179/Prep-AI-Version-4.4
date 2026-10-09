"""Counts database round trips per page (a click on Supabase costs ~20-80 ms per query). Run: python tests/perf_probe.py"""
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


import db, db_core
_memo = {}
def _cache_resource(*a, **k):
    def wrap(f):
        def inner(*args, **kw):
            key = (f.__qualname__, args)
            if key not in _memo: _memo[key] = f(*args, **kw)
            return _memo[key]
        return inner
    return wrap(a[0]) if a and callable(a[0]) else wrap
FakeSt.cache_resource = staticmethod(_cache_resource)    # like Streamlit: runs once per server process
run("Dashboard"); db.ensure_student("stu1", "Ali")
PAGES = ["Dashboard", "Today", "Daily Challenge", "Live Quiz", "Learn", "Practice", "Exam", "Mock Test", "My Classes", "Published Quizzes", "AI Tutor", "History", "Memory", "Settings"]
def probe(page, **st):
    run(page, **st); db_core.STATS.update(connects=0, queries=0); run(page, **st)   # second run = a normal click (caches warm)
    return db_core.STATS["connects"], db_core.STATS["queries"]
rows = [(p, *probe(p)) for p in PAGES]
rows.append(("Tutor Dashboard", *probe("Tutor Dashboard", is_tutor=True)))
print(f"{'page':22}{'connects':>9}{'queries':>9}")
for r in rows: print(f"{r[0]:22}{r[1]:>9}{r[2]:>9}")
print("TOTAL queries", sum(r[2] for r in rows))
