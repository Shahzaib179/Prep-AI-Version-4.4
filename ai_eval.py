"""AI test set: measures how well the AI behind Prep AI answers, writes questions and stays grounded.

Three suites, all running against the real prompts of the app and a fixed, hand-checked data set in evals/:
  solver    - the model answers the 40 golden MCQs (answer keys are known)            -> accuracy
  generate  - the Assessment agent writes MCQs from short source passages; we check structure, duplicates, grounding, and whether a
              SECOND blind call (without the key) agrees with the answer key          -> validity / agreement
  grounded  - the tutor answers questions from a passage: answerable ones must contain the fact, unanswerable ones must say the
              sources do not contain it                                               -> grounded accuracy / abstention
The model is injected, so the scoring code is tested without internet. Pure logic (no Streamlit).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import question_tools as qt

EVAL_DIR = Path(__file__).resolve().parent / "evals"
THRESHOLDS = {"solver": 85.0, "generate_valid": 90.0, "generate_agree": 80.0, "generate_grounded": 70.0, "grounded": 75.0}
ABSTAIN = re.compile(r"not (?:in|found in|mentioned|covered|provided|stated|available|included)|does(?:n't| not) (?:mention|contain|say|state|provide|include|specify|cover)|no (?:information|mention|details)|cannot (?:find|answer)|can't (?:find|answer)|isn't (?:in|mentioned)|unable to find|not enough information|outside (?:the|these) (?:provided )?(?:sources|context)", re.I)


def load_golden() -> list[dict[str, Any]]:
    return json.loads((EVAL_DIR / "golden_questions.json").read_text(encoding="utf-8"))


def load_contexts() -> list[dict[str, Any]]:
    return json.loads((EVAL_DIR / "contexts.json").read_text(encoding="utf-8"))


def pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


# ------------------------------------------------------------------ parsing
def parse_letter(raw: Any) -> str | None:
    """Pull the chosen option letter out of a model reply ('B', '{"answer":"b"}', 'Answer: C. ...')."""
    if isinstance(raw, dict):
        raw = raw.get("answer") or raw.get("choice") or ""
    text = str(raw or "").strip()
    m = re.search(r'"answer"\s*:\s*"?\s*([A-Da-d])\b', text) or re.match(r"^\W*([A-Da-d])\b", text) or re.search(r"(?:answer|option|choice)\s*(?:is|:)?\s*\(?([A-Da-d])\b", text, re.I)
    return m.group(1).upper() if m else None


def _blind_prompt(q: dict[str, Any]) -> str:
    opts = "\n".join(f"{k}. {q['options'][k]}" for k in "ABCD")
    return f"Answer this multiple-choice question. Reply with JSON only: {{\"answer\": \"A|B|C|D\"}}.\n\n{q['question']}\n{opts}"


# ------------------------------------------------------------------ suites
def run_solver(ask_json: Callable[[str], Any], golden: list[dict[str, Any]] | None = None, limit: int | None = None) -> dict[str, Any]:
    golden = (golden or load_golden())[:limit]
    by_subject: dict[str, list[int]] = {}
    wrong = []
    for q in golden:
        try:
            got = parse_letter(ask_json(_blind_prompt(q)))
        except Exception as exc:  # noqa: BLE001
            got = None
            err = str(exc)[:120]
        ok = got == q["answer"]
        by_subject.setdefault(q["subject"], []).append(int(ok))
        if not ok:
            wrong.append({"id": q["id"], "question": q["question"], "expected": q["answer"], "got": got})
    total = sum(len(v) for v in by_subject.values())
    return {"suite": "solver", "score": pct(sum(sum(v) for v in by_subject.values()), total), "n": total,
            "by_subject": {s: pct(sum(v), len(v)) for s, v in by_subject.items()}, "wrong": wrong}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}


def grounded_overlap(q: dict[str, Any], context: str) -> float:
    """Share of the key terms in the question + correct option that also appear in the source passage (0-1)."""
    terms = _tokens(q["question"] + " " + q["options"].get(q["answer"], ""))
    return len(terms & _tokens(context)) / len(terms) if terms else 0.0


def run_generate(generate: Callable[[dict[str, Any], int], list[dict[str, Any]]], ask_json: Callable[[str], Any],
                 contexts: list[dict[str, Any]] | None = None, per_context: int = 5, blind_check: bool = True) -> dict[str, Any]:
    contexts = contexts or load_contexts()
    requested = produced = valid = agree = checked = grounded = 0
    letters: Counter = Counter()
    problems: list[str] = []
    seen: set[str] = set()
    dupes = 0
    for c in contexts:
        requested += per_context
        try:
            items = generate(c, per_context) or []
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{c['id']}: generation failed ({str(exc)[:100]})")
            continue
        for q in items:
            produced += 1
            qn = qt.normalize_question(q)
            errs = qt.validate_question(qn)
            if errs:
                problems.append(f"{c['id']}: {errs[0]}")
                continue
            valid += 1
            letters[qn["answer"]] += 1
            key = re.sub(r"\W+", " ", qn["question"].lower()).strip()
            if key in seen:
                dupes += 1
            seen.add(key)
            if grounded_overlap(qn, c["text"]) >= 0.5:
                grounded += 1
            if blind_check:
                checked += 1
                try:
                    agree += int(parse_letter(ask_json(_blind_prompt(qn))) == qn["answer"])
                except Exception:  # noqa: BLE001
                    pass
    top_letter_share = pct(max(letters.values()), valid) if letters else 0.0
    return {"suite": "generate", "requested": requested, "produced": produced, "valid_pct": pct(valid, produced), "completeness_pct": pct(valid, requested),
            "agreement_pct": pct(agree, checked), "grounded_pct": pct(grounded, valid), "duplicates": dupes,
            "answer_letter_share_max_pct": top_letter_share, "problems": problems[:10]}


def run_grounded(answer: Callable[[str, str], str], contexts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    contexts = contexts or load_contexts()
    ans_ok = ans_n = abs_ok = abs_n = 0
    misses = []
    for c in contexts:
        for p in c["probes"]:
            try:
                reply = str(answer(p["q"], c["text"]) or "")
            except Exception as exc:  # noqa: BLE001
                reply = f"[error] {exc}"
            low = reply.lower().replace(" ", "")
            if p["expect"] is None:                       # not in the passage: the right behaviour is to say so
                abs_n += 1
                good = bool(ABSTAIN.search(reply))
                abs_ok += int(good)
            else:
                ans_n += 1
                good = any(e.lower().replace(" ", "") in low for e in p["expect"])
                ans_ok += int(good)
            if not good:
                misses.append({"context": c["id"], "question": p["q"], "kind": "abstain" if p["expect"] is None else "answer", "reply": reply[:160]})
    return {"suite": "grounded", "score": pct(ans_ok + abs_ok, ans_n + abs_n), "answerable_pct": pct(ans_ok, ans_n), "abstain_pct": pct(abs_ok, abs_n), "n": ans_n + abs_n, "misses": misses}


# ------------------------------------------------------------------ report
def verdicts(results: dict[str, dict[str, Any]]) -> dict[str, bool]:
    v = {}
    if "solver" in results:
        v["solver"] = results["solver"]["score"] >= THRESHOLDS["solver"]
    if "generate" in results:
        g = results["generate"]
        v["generate_valid"] = g["valid_pct"] >= THRESHOLDS["generate_valid"]
        v["generate_agree"] = g["agreement_pct"] >= THRESHOLDS["generate_agree"]
        v["generate_grounded"] = g["grounded_pct"] >= THRESHOLDS["generate_grounded"]
    if "grounded" in results:
        v["grounded"] = results["grounded"]["score"] >= THRESHOLDS["grounded"]
    return v


def headline(results: dict[str, dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    if "solver" in results:
        out["Solver accuracy %"] = results["solver"]["score"]
    if "generate" in results:
        g = results["generate"]
        out.update({"Generated: valid %": g["valid_pct"], "Generated: key agrees %": g["agreement_pct"], "Generated: grounded %": g["grounded_pct"]})
    if "grounded" in results:
        out["Grounded answers %"] = results["grounded"]["score"]
    return out


def compare(current: dict[str, float], previous: dict[str, float] | None, tolerance: float = 5.0) -> list[str]:
    """Metrics that dropped by more than ``tolerance`` points since the previous run."""
    if not previous:
        return []
    return [f"{k}: {previous[k]} -> {v}" for k, v in current.items() if k in previous and v < previous[k] - tolerance]


def to_markdown(model: str, results: dict[str, dict[str, Any]], regressions: list[str] | None = None) -> str:
    v = verdicts(results)
    lines = [f"# AI quality report ({model})", f"_Run {datetime.utcnow():%Y-%m-%d %H:%M} UTC_", "", "| Metric | Score | Target | Result |", "|---|---|---|---|"]
    targets = {"Solver accuracy %": ("solver", THRESHOLDS["solver"]), "Generated: valid %": ("generate_valid", THRESHOLDS["generate_valid"]),
               "Generated: key agrees %": ("generate_agree", THRESHOLDS["generate_agree"]), "Generated: grounded %": ("generate_grounded", THRESHOLDS["generate_grounded"]),
               "Grounded answers %": ("grounded", THRESHOLDS["grounded"])}
    for name, score in headline(results).items():
        key, target = targets[name]
        lines.append(f"| {name} | {score} | ≥ {target} | {'PASS' if v.get(key) else 'FAIL'} |")
    if regressions:
        lines += ["", "**Dropped since the last run:** " + "; ".join(regressions)]
    for r in results.values():
        for key in ("wrong", "misses", "problems"):
            if r.get(key):
                lines += ["", f"## {r['suite']}: {key}"] + [f"- {json.dumps(x, ensure_ascii=False)}" for x in r[key][:10]]
    return "\n".join(lines) + "\n"


def save_run(model: str, results: dict[str, dict[str, Any]]) -> int:
    from db_core import connect
    summary = {"headline": headline(results), "verdicts": verdicts(results), "results": results}
    with connect() as con:
        return con.insert("INSERT INTO ai_eval_runs(created_at,model,summary_json) VALUES(?,?,?)", (datetime.utcnow().isoformat(), model, json.dumps(summary)))


def recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    from db_core import connect
    with connect() as con:
        rows = con.execute("SELECT id,created_at,model,summary_json FROM ai_eval_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"], "model": r["model"], **json.loads(r["summary_json"])} for r in rows]
