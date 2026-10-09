"""Month 2: daily challenge, live quiz, WhatsApp reminders, AI test set."""
import json, pathlib, sys, tempfile, types, unittest
from datetime import date, datetime, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
class _St(types.ModuleType):
    session_state = {}; secrets = {}
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
sys.modules["streamlit"] = _St("streamlit")
import config; config.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "m2.db"
import ai_eval, bank, daily, db, live, progress, reminders  # noqa: E402
import question_tools as qt  # noqa: E402
db.init_db()
n = [0]
def student(name=None):
    n[0] += 1; s = f"m2s{n[0]}"; db.ensure_student(s, name or s); return s
def Q(i, ans="A"):
    return {"question": f"Live question number {i} about cells?", "options": {"A": f"a{i}", "B": f"b{i}", "C": f"c{i}", "D": f"d{i}"}, "answer": ans, "explanation": "because", "concept": "c", "difficulty": "Medium"}


class Daily(unittest.TestCase):
    def test_works_offline_from_starter_set_and_is_the_same_for_everyone(self):
        d = date(2030, 1, 5)
        a = daily.get_challenge(d); b = daily.get_challenge(d)
        self.assertEqual(len(a), 5); self.assertEqual(a, b)
        self.assertEqual([q["subject"] for q in a].count("Biology"), 2)
        for q in a: self.assertEqual(qt.validate_question(q), [])

    def test_different_days_differ_and_ai_is_only_a_helper(self):
        calls = []
        def gen(subject, k): calls.append(subject); raise RuntimeError("AI down")
        q1 = daily.get_challenge(date(2030, 2, 1), gen); self.assertEqual(len(q1), 5)
        self.assertNotEqual([q["question"] for q in q1], [q["question"] for q in daily.get_challenge(date(2030, 2, 2))])

    def test_shared_bank_questions_are_preferred(self):
        t = student()
        for i in range(4): bank.add_question(t, Q(100 + i), "Biology", "T", "manual", shared=True)
        qs = daily.build_questions(date(2030, 3, 1))
        bio = [q for q in qs if q["subject"] == "Biology"]
        self.assertTrue(all("Live question" in q["question"] for q in bio))

    def test_one_result_per_day_bonus_and_leaderboard(self):
        a, b, c = student("Ann"), student("Bob"), student("Cy"); d = date(2030, 4, 1)
        self.assertEqual(daily.record_result(a, d, 5, 5, 60), daily.BONUS_XP + daily.BONUS_PERFECT)
        self.assertEqual(daily.record_result(a, d, 1, 5, 10), 0)                      # second attempt ignored
        daily.record_result(b, d, 4, 5, 50); daily.record_result(c, d, 4, 5, 40)
        board = daily.leaderboard(d)
        self.assertEqual([r["name"] for r in board], ["Ann", "Cy", "Bob"])             # score, then faster time
        self.assertEqual(daily.rank_of(b, d), (3, 3))
        self.assertEqual(daily.result_for(a, d)["correct"], 5)

    def test_bonus_xp_counts_without_adding_questions(self):
        s = student(); progress.add_bonus_xp(s, 30)
        p = progress.get_progress(s); self.assertEqual(p["total_xp"], 30); self.assertEqual(p["today_questions"], 0)

    def test_student_sees_own_order_but_same_questions(self):
        qs = daily.get_challenge(date(2030, 5, 1)); d = date(2030, 5, 1)
        v1, v2 = daily.for_student(qs, "x1", d), daily.for_student(qs, "x2", d)
        self.assertEqual({q["question"] for q in v1}, {q["question"] for q in v2})
        for q in v1:
            orig = next(o for o in qs if o["question"] == q["question"])
            self.assertEqual(q["options"][q["answer"]], orig["options"][orig["answer"]])

    def test_calendar_grid(self):
        today_ = date(2030, 6, 12)  # a Wednesday
        g = daily.calendar_grid({date(2030, 6, 10), date(2030, 6, 12)}, today_, 3)
        self.assertEqual(len(g), 3); self.assertTrue(all(len(w) == 7 for w in g))
        self.assertEqual(g[-1][0]["day"], date(2030, 6, 10)); self.assertTrue(g[-1][0]["active"])
        self.assertTrue(g[-1][2]["is_today"]); self.assertTrue(g[-1][3]["future"])


class Live(unittest.TestCase):
    def setUp(self):
        self.host = student("Host"); self.s1, self.s2 = student("One"), student("Two")
        self.made = live.create_session(self.host, "T", [Q(1, "A"), Q(2, "B")], 20)
        self.code, self.sid = self.made["code"], self.made["id"]

    def test_full_game(self):
        self.assertTrue(live.join(self.code, self.s1, "One")[0]); self.assertTrue(live.join(self.code, self.s2, "Two")[0])
        self.assertFalse(live.join(self.code, self.host, "Host")[0])
        self.assertEqual(live.get_state(self.code)["state"], live.LOBBY)
        self.assertEqual(live.advance(self.code, self.host, -1, t=1000.0), live.QUESTION)
        row = live.get_state(self.code)
        self.assertEqual(live.phase(row, 1005.0), live.QUESTION); self.assertEqual(live.phase(row, 1021.0), live.REVEAL)
        r1 = live.submit_answer(self.sid, self.s1, 0, "A", t=1002.0); r2 = live.submit_answer(self.sid, self.s2, 0, "A", t=1018.0)
        self.assertTrue(r1["correct"] and r2["correct"]); self.assertGreater(r1["points"], r2["points"])
        self.assertEqual(r1["points"], live.points_for(True, 2.0, 20))
        self.assertFalse(live.submit_answer(self.sid, self.s1, 0, "B", t=1003.0)["accepted"])      # one answer per question
        self.assertEqual(live.answer_counts(self.sid, 0), {"A": 2})
        live.advance(self.code, self.host, 0, t=1030.0)
        self.assertFalse(live.submit_answer(self.sid, self.s1, 0, "A", t=1031.0)["accepted"])      # old question closed
        live.submit_answer(self.sid, self.s1, 1, "C", t=1032.0)                                      # wrong
        self.assertEqual(live.advance(self.code, self.host, 1, t=1060.0), live.FINISHED)
        board = live.standings(self.sid)
        self.assertEqual([p["student_id"] for p in board], [self.s1, self.s2]); self.assertEqual(board[0]["correct"], 1)
        rep = live.final_report(self.sid); self.assertEqual(rep[0]["total"], 2); self.assertEqual(rep[0]["accuracy"], 50.0)

    def test_late_and_unauthorised_actions_are_refused(self):
        live.join(self.code, self.s1, "One"); live.advance(self.code, self.host, -1, t=2000.0)
        self.assertFalse(live.submit_answer(self.sid, self.s1, 0, "A", t=2000.0 + 20 + live.GRACE_SEC + 0.5)["accepted"])
        self.assertTrue(live.submit_answer(self.sid, self.s1, 0, "A", t=2000.0 + 20 + 1.0)["accepted"])   # inside the grace period
        self.assertEqual(live.advance(self.code, self.s1), "denied")                                       # a student cannot run the game
        self.assertFalse(live.submit_answer(self.sid, self.s2, 0, "A", t=2001.0)["accepted"])              # not joined
        self.assertEqual(live.advance(self.code, self.host, 7), live.QUESTION)                             # stale double-click ignored

    def test_cannot_join_finished_and_double_next_is_harmless(self):
        live.join(self.code, self.s1, "One"); live.advance(self.code, self.host, -1, t=1.0)
        live.advance(self.code, self.host, 0, t=2.0); live.advance(self.code, self.host, 0, t=3.0)
        self.assertEqual(live.get_state(self.code)["q_index"], 1)
        live.end_session(self.code, self.host)
        self.assertFalse(live.join(self.code, self.s2, "Two")[0])

    def test_player_sees_shuffled_options_and_answer_maps_back(self):
        q = Q(5, "C"); live_q = live.display_question(q, self.s1, self.sid)
        letter = next(k for k, v in live_q["options"].items() if v == q["options"]["C"])
        self.assertEqual(live.original_letter(q, live_q, letter), "C")
        self.assertEqual(live_q["answer"], letter)

    def test_credit_goes_through_normal_progress_once(self):
        live.join(self.code, self.s1, "One"); live.advance(self.code, self.host, -1, t=1.0)
        live.submit_answer(self.sid, self.s1, 0, "A", t=2.0)
        live.finish_credit(self.sid, self.s1); live.finish_credit(self.sid, self.s1)
        self.assertEqual(len(db.history(self.s1)), 1)
        self.assertGreater(progress.get_progress(self.s1)["total_xp"], 0)


class Reminders(unittest.TestCase):
    def test_phone_numbers(self):
        self.assertEqual(reminders.normalize_phone("0300 1234567"), "+923001234567")
        self.assertEqual(reminders.normalize_phone("+92 (300) 123-4567"), "+923001234567")
        self.assertEqual(reminders.normalize_phone("00923001234567"), "+923001234567")
        for bad in ("", "abc", "12345", "+0123456789012"): self.assertIsNone(reminders.normalize_phone(bad))

    def test_opt_in_needs_valid_number_and_consent(self):
        s = student()
        self.assertFalse(reminders.save_prefs(s, "123", True, 19, ["streak"], True)[0])
        self.assertFalse(reminders.save_prefs(s, "03001234567", True, 19, ["streak"], False)[0])
        self.assertFalse(reminders.get_prefs(s)["enabled"])
        self.assertTrue(reminders.save_prefs(s, "03001234567", True, 19, ["streak", "daily"], True)[0])
        p = reminders.get_prefs(s); self.assertTrue(p["enabled"] and p["consented_at"]); self.assertEqual(p["phone"], "+923001234567")
        reminders.disable(s); self.assertFalse(reminders.get_prefs(s)["enabled"])

    def test_message_only_when_useful(self):
        s = student("Sara Khan")
        msg = reminders.compose(s, name="Sara Khan"); self.assertIn("Hi Sara", msg); self.assertIn("Daily Challenge", msg)
        daily.record_result(s, progress.local_today(), 3, 5, 60)
        db.record_quiz(s, "Biology", "Cells", [Q(1)], {0: "A"}, "Medium")
        self.assertIsNone(reminders.compose(s, name="Sara"))
        self.assertIsNone(reminders.compose(s, kinds=["revision"]))

    def test_streak_at_risk_message(self):
        s = student()
        with db.connect() as con:
            for back in (1, 2, 3):
                con.execute("INSERT INTO daily_activity(student_id,day,questions,correct,quizzes,xp) VALUES(?,?,10,5,1,30)", (s, (progress.local_today() - timedelta(days=back)).isoformat()))
        self.assertIn("3-day streak", reminders.compose(s, kinds=["streak"]))

    def _enable(self, s, hour=19):
        self.assertTrue(reminders.save_prefs(s, "03001234567", True, hour, ["daily"], True)[0])

    def test_scheduler_sends_once_a_day_after_the_chosen_hour(self):
        s = student(); self._enable(s, 19)
        with db.connect() as con: con.execute("DELETE FROM reminder_prefs WHERE student_id<>?", (s,))
        sent = []
        def http(url, data, headers): sent.append((url, data)); return 201, "ok"
        import os; os.environ.update(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok", TWILIO_WHATSAPP_FROM="+14155238886")
        local = lambda h: datetime(2031, 1, 1, h, 7) - timedelta(minutes=config.PROGRESS_TZ_OFFSET_MIN)   # UTC time that is h:07 locally
        self.assertEqual(reminders.run_due("twilio", local(18), http)["sent"], 0)                         # too early
        r = reminders.run_due("twilio", local(19), http); self.assertEqual((r["sent"], r["failed"]), (1, 0))
        self.assertIn(b"whatsapp%3A%2B923001234567", sent[0][1])
        self.assertEqual(reminders.run_due("twilio", local(20), http)["sent"], 0)                         # already sent today
        self.assertEqual(reminders.run_due("twilio", local(23), http)["candidates"], 0)                   # never after 22:00

    def test_failures_retry_twice_then_stop_and_opt_out_is_respected(self):
        s = student(); self._enable(s, 8)
        bad = lambda url, data, headers: (500, "boom")
        loc = lambda h: datetime(2031, 2, 1, h, 0) - timedelta(minutes=config.PROGRESS_TZ_OFFSET_MIN)
        import os; os.environ.update(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok", TWILIO_WHATSAPP_FROM="+14155238886")
        for h in (8, 9, 10, 11): reminders.run_due("twilio", loc(h), bad)
        with db.connect() as con: self.assertEqual(con.execute("SELECT COUNT(*) FROM reminder_log WHERE student_id=? AND status='failed'", (s,)).fetchone()[0], 2)
        s2 = student(); self._enable(s2, 8); reminders.disable(s2)
        self.assertNotIn(s2, [x["student_id"] for x in reminders.due_students(loc(9))])

    def test_provider_settings_missing_is_a_clear_failure_and_dry_run_sends_nothing(self):
        import os
        for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "WA_CLOUD_TOKEN", "WA_PHONE_NUMBER_ID"): os.environ.pop(k, None)
        self.assertFalse(reminders.send_message("twilio", "+923001234567", "x")[0])
        self.assertFalse(reminders.send_message("cloudapi", "+923001234567", "x")[0])
        self.assertTrue(reminders.send_message("dry_run", "+923001234567", "x")[0])
        self.assertIn("wa.me/923001234567?text=", reminders.wa_link("hello world", "+923001234567"))

    def test_cloud_api_uses_template_when_configured(self):
        import os; os.environ.update(WA_CLOUD_TOKEN="t", WA_PHONE_NUMBER_ID="123", WA_TEMPLATE_NAME="study_reminder")
        seen = []
        ok, _ = reminders.send_message("cloudapi", "+923001234567", "line1\nline2", lambda u, d, h: (seen.append(json.loads(d)) or (200, "ok")))
        self.assertTrue(ok); self.assertEqual(seen[0]["type"], "template"); self.assertEqual(seen[0]["to"], "923001234567")
        self.assertNotIn("\n", seen[0]["template"]["components"][0]["parameters"][0]["text"])


class AiTestSet(unittest.TestCase):
    def test_golden_set_is_valid_and_balanced(self):
        g = ai_eval.load_golden()
        self.assertGreaterEqual(len(g), 40); self.assertEqual(len({q["id"] for q in g}), len(g)); self.assertEqual(len({q["question"] for q in g}), len(g))
        for q in g: self.assertEqual(qt.validate_question(q), [], q["id"])
        letters = {q["answer"] for q in g}; self.assertEqual(letters, {"A", "B", "C", "D"})
        for c in ai_eval.load_contexts(): self.assertTrue(any(p["expect"] is None for p in c["probes"]) and any(p["expect"] for p in c["probes"]))

    def test_parse_letter(self):
        for raw, want in (("B", "B"), ('{"answer": "c"}', "C"), ({"answer": "D"}, "D"), ("Answer: A. because", "A"), ("The answer is b", "B"), ("none", None), ("", None)):
            self.assertEqual(ai_eval.parse_letter(raw), want, raw)

    def test_solver_scores_a_perfect_and_a_bad_model(self):
        g = ai_eval.load_golden(); key = {q["question"]: q["answer"] for q in g}
        def perfect(prompt): return {"answer": next(a for qn, a in key.items() if qn in prompt)}
        self.assertEqual(ai_eval.run_solver(perfect)["score"], 100.0)
        res = ai_eval.run_solver(lambda p: {"answer": "A"}); self.assertEqual(res["score"], ai_eval.pct(sum(q["answer"] == "A" for q in g), len(g)))
        self.assertTrue(res["wrong"]); self.assertLess(ai_eval.run_solver(lambda p: (_ for _ in ()).throw(RuntimeError("down")))["score"], 1)

    def test_generation_checks_structure_duplicates_grounding_and_agreement(self):
        ctxs = ai_eval.load_contexts()[:1]; c = ctxs[0]
        good = {"question": "Where does the Krebs cycle take place in the mitochondrion?", "options": {"A": "Mitochondrial matrix", "B": "Cytoplasm", "C": "Nucleus", "D": "Ribosome"}, "answer": "A", "explanation": "x", "concept": "m"}
        broken = {"question": "short", "options": {"A": "1"}, "answer": "Z"}
        res = ai_eval.run_generate(lambda ctx, k: [good, dict(good), broken], lambda p: {"answer": "A"}, ctxs, per_context=3)
        self.assertEqual((res["produced"], res["duplicates"]), (3, 1)); self.assertEqual(res["valid_pct"], 66.7)
        self.assertEqual(res["agreement_pct"], 100.0); self.assertEqual(res["grounded_pct"], 100.0)
        wrong_key = ai_eval.run_generate(lambda ctx, k: [good], lambda p: {"answer": "B"}, ctxs, per_context=1)
        self.assertEqual(wrong_key["agreement_pct"], 0.0)
        self.assertTrue(ai_eval.run_generate(lambda ctx, k: 1 / 0, lambda p: "A", ctxs)["problems"])

    def test_grounded_suite_rewards_facts_and_honest_abstention(self):
        ctxs = ai_eval.load_contexts()
        def good(q, ctx):
            for c in ctxs:
                for p in c["probes"]:
                    if p["q"] == q: return ("It is not available in the provided material." if p["expect"] is None else "Answer: " + " ".join(p["expect"]))
        self.assertEqual(ai_eval.run_grounded(good)["score"], 100.0)
        hallucinator = ai_eval.run_grounded(lambda q, ctx: "It is exactly 42.")
        self.assertEqual(hallucinator["abstain_pct"], 0.0); self.assertTrue(hallucinator["misses"])

    def test_report_targets_regressions_and_storage(self):
        res = {"solver": {"suite": "solver", "score": 90.0, "n": 40, "by_subject": {}, "wrong": []}, "grounded": {"suite": "grounded", "score": 60.0, "n": 18, "misses": []}}
        v = ai_eval.verdicts(res); self.assertTrue(v["solver"]); self.assertFalse(v["grounded"])
        self.assertEqual(ai_eval.compare({"Solver accuracy %": 80.0}, {"Solver accuracy %": 90.0}), ["Solver accuracy %: 90.0 -> 80.0"])
        self.assertEqual(ai_eval.compare({"x": 88.0}, {"x": 90.0}), [])
        self.assertIn("FAIL", ai_eval.to_markdown("m", res))
        ai_eval.save_run("m", res); self.assertEqual(ai_eval.recent_runs(1)[0]["headline"]["Solver accuracy %"], 90.0)

    def test_starter_pack_imports_into_a_bank_without_duplicates(self):
        t = student(); g = ai_eval.load_golden()
        self.assertEqual(bank.bulk_add(t, g)["added"], len(g)); self.assertEqual(bank.bulk_add(t, g)["duplicates"], len(g))


if __name__ == "__main__":
    unittest.main(verbosity=1)
