"""Retry / fallback / limiter / cache behaviour of ai_gateway (no network, no sleeping)."""
import pathlib, sys, threading, time, types, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ai_gateway as g

class Err(Exception):
    def __init__(self, msg, status=None, headers=None):
        super().__init__(msg); self.status_code = status
        if headers is not None: self.response = types.SimpleNamespace(headers=headers, status_code=status)

def scripted(*steps):
    """steps: exception instances (raised) or values (returned), consumed one per call."""
    calls = []
    it = iter(steps)
    def call(model):
        calls.append(model); step = next(it)
        if isinstance(step, BaseException): raise step
        return step
    return call, calls

MODELS = ["big", "small"]
NOSLEEP = dict(sleep=lambda s: SLEPT.append(s), rng=lambda: 0.5)
SLEPT = []

class Gateway(unittest.TestCase):
    def setUp(self): SLEPT.clear(); g.STATS.__init__(); g._CACHE.clear()

    def test_success_first_try(self):
        call, calls = scripted("ok")
        self.assertEqual(g.run_with_resilience(call, MODELS, **NOSLEEP), ("ok", "big")); self.assertEqual(calls, ["big"]); self.assertEqual(SLEPT, [])

    def test_rate_limit_is_retried_with_exponential_backoff(self):
        call, calls = scripted(Err("rate limit", 429), Err("rate limit", 429), "ok")
        self.assertEqual(g.run_with_resilience(call, MODELS, **NOSLEEP), ("ok", "big"))
        self.assertEqual(calls, ["big", "big", "big"]); self.assertEqual(len(SLEPT), 2); self.assertLess(SLEPT[0], SLEPT[1])   # 1s then 2s (x jitter)
        self.assertEqual(g.STATS.retries, 2)

    def test_retry_after_header_is_honoured(self):
        call, _ = scripted(Err("429", 429, {"retry-after": "7"}), "ok")
        g.run_with_resilience(call, MODELS, **NOSLEEP); self.assertEqual(SLEPT, [7.0])

    def test_falls_back_to_second_model_after_retries_exhausted(self):
        call, calls = scripted(*[Err("rate limit", 429)] * 4, "from small")      # 1 try + 3 retries on big, then small works
        self.assertEqual(g.run_with_resilience(call, MODELS, **NOSLEEP), ("from small", "small"))
        self.assertEqual(calls, ["big"] * 4 + ["small"]); self.assertEqual(g.STATS.fallbacks, 1)

    def test_everything_down_raises_friendly_error(self):
        call, _ = scripted(*[Err("rate limit", 429)] * 8)
        with self.assertRaises(g.AIUnavailable) as cm: g.run_with_resilience(call, MODELS, **NOSLEEP)
        self.assertEqual(cm.exception.kind, g.RATE_LIMIT); self.assertIn("busy", str(cm.exception)); self.assertNotIn("429", str(cm.exception))

    def test_bad_key_stops_immediately(self):
        call, calls = scripted(Err("Invalid API Key", 401), "never")
        with self.assertRaises(g.AIUnavailable) as cm: g.run_with_resilience(call, MODELS, **NOSLEEP)
        self.assertEqual((cm.exception.kind, calls, SLEPT), (g.AUTH, ["big"], []))

    def test_forbidden_model_skips_to_next_without_waiting(self):
        call, calls = scripted(Err("model blocked", 403), "ok")
        self.assertEqual(g.run_with_resilience(call, MODELS, **NOSLEEP), ("ok", "small")); self.assertEqual(SLEPT, [])

    def test_too_large_is_not_retried(self):
        call, calls = scripted(Err("Request too large", 413))
        with self.assertRaises(g.AIUnavailable) as cm: g.run_with_resilience(call, MODELS, **NOSLEEP)
        self.assertEqual((cm.exception.kind, calls), (g.TOO_LARGE, ["big"]))

    def test_server_errors_and_timeouts_are_retried(self):
        class ReadTimeout(Exception): pass
        call, calls = scripted(Err("bad gateway", 502), ReadTimeout("timed out"), "ok")
        self.assertEqual(g.run_with_resilience(call, MODELS, **NOSLEEP)[0], "ok"); self.assertEqual(len(calls), 3)

    def test_unknown_error_gets_one_retry_then_fallback(self):
        call, calls = scripted(ValueError("weird"), ValueError("weird"), "ok")
        self.assertEqual(g.run_with_resilience(call, MODELS, **NOSLEEP), ("ok", "small")); self.assertEqual(calls, ["big", "big", "small"])

    def test_concurrency_limit(self):
        active, peak, lock = [0], [0], threading.Lock()
        def call(model):
            with lock: active[0] += 1; peak[0] = max(peak[0], active[0])
            time.sleep(0.03)
            with lock: active[0] -= 1
            return "ok"
        ts = [threading.Thread(target=lambda: g.run_with_resilience(call, MODELS)) for _ in range(12)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertLessEqual(peak[0], g.MAX_CONCURRENT_CALLS); self.assertGreater(peak[0], 1)

    def test_cache_hits_expire_and_are_bounded(self):
        k = g.cache_key("m", "prompt"); clock = [100.0]
        g.cache_put(k, "answer", now=lambda: clock[0])
        self.assertEqual(g.cache_get(k, 60, now=lambda: clock[0] + 30), "answer")
        self.assertIsNone(g.cache_get(k, 60, now=lambda: clock[0] + 90))
        self.assertIsNone(g.cache_get(k, 0))                                           # ttl 0 = caching off
        for i in range(g.CACHE_MAX_ITEMS + 20): g.cache_put(f"k{i}", i)
        self.assertLessEqual(len(g._CACHE), g.CACHE_MAX_ITEMS)


class GroqServiceIntegration(unittest.TestCase):
    """groq_service.generate_text wired to the gateway, with a fake Groq client."""
    @classmethod
    def setUpClass(cls):
        class _St(types.ModuleType):
            session_state = {}; secrets = {"GROQ_API_KEY": "k"}
            def __getattr__(self, n):
                if n.startswith("__"): raise AttributeError(n)
                return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
        sys.modules["streamlit"] = _St("streamlit")
        gm = types.ModuleType("groq"); gm.Groq = object; sys.modules["groq"] = gm
        import groq_service; cls.gs = groq_service

    def _client(self, behaviour):
        seen = []
        def create(**kw):
            seen.append(kw["model"]); r = behaviour(kw["model"], len(seen))
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=r))])
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))), seen

    def test_backup_model_answers_when_primary_is_rate_limited(self):
        def behaviour(model, n):
            if model == "openai/gpt-oss-120b": raise Err("rate limit", 429)
            return "backup answer"
        client, seen = self._client(behaviour)
        self.gs.get_client = lambda key: client
        orig = g.time.sleep; g.time.sleep = lambda s: None
        try:
            self.assertEqual(self.gs.generate_text("hello"), "backup answer")
        finally: g.time.sleep = orig
        self.assertEqual(seen[-1], "openai/gpt-oss-20b"); self.assertIn("backup", sys.modules["streamlit"].session_state["ai_fallback_notice"])

    def test_total_failure_becomes_a_safe_service_error(self):
        client, _ = self._client(lambda m, n: (_ for _ in ()).throw(Err("Invalid API Key", 401)))
        self.gs.get_client = lambda key: client
        with self.assertRaises(self.gs.GroqServiceError) as cm: self.gs.generate_text("hello")
        self.assertIn("API key", str(cm.exception))

    def test_cache_ttl_reuses_answer(self):
        client, seen = self._client(lambda m, n: f"answer {n}")
        self.gs.get_client = lambda key: client; g._CACHE.clear()
        a = self.gs.generate_text("same prompt", cache_ttl=60); b = self.gs.generate_text("same prompt", cache_ttl=60)
        self.assertEqual((a, b, len(seen)), ("answer 1", "answer 1", 1))
        self.gs.generate_text("same prompt"); self.assertEqual(len(seen), 2)             # no ttl = always fresh


if __name__ == "__main__":
    unittest.main(verbosity=1)
