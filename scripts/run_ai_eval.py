"""Run the AI test set against the live Groq models and print a report. Needs GROQ_API_KEY (environment or .streamlit/secrets.toml).

  python scripts/run_ai_eval.py                       # everything (about 40 + 60 + 18 model calls)
  python scripts/run_ai_eval.py --suite solver        # one suite: solver | generate | grounded
  python scripts/run_ai_eval.py --limit 10            # first 10 golden questions only
  python scripts/run_ai_eval.py --save                # also store the result in the database (shown on the tutor's AI Quality tab)
Exit code 1 when a metric is below its target, so it can gate a release.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ai_eval  # noqa: E402
import groq_service  # noqa: E402
from agent_system import AgentContext, AssessmentAgent  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", action="append", choices=["solver", "generate", "grounded"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--per-context", type=int, default=5)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--out", default="ai_eval_report.md")
    args = ap.parse_args()
    suites = args.suite or ["solver", "generate", "grounded"]
    model = groq_service.current_model()
    agent = AssessmentAgent()

    def ask_json(prompt: str):
        return groq_service.generate_json(prompt)

    def generate(c: dict, n: int):
        ctx = AgentContext(student_id="eval", request=f"Create {n} MCQs", subject=c["subject"], topic=c["topic"], difficulty="Medium", rag_context=c["text"])
        return agent.validate(agent.generate_mcqs(ctx, [], n), c["text"], c["topic"])

    results = {}
    if "solver" in suites:
        results["solver"] = ai_eval.run_solver(ask_json, limit=args.limit)
    if "generate" in suites:
        results["generate"] = ai_eval.run_generate(generate, ask_json, per_context=args.per_context)
    if "grounded" in suites:
        results["grounded"] = ai_eval.run_grounded(lambda q, ctx: groq_service.grounded_answer(q, ctx))
    previous = None
    try:
        import db
        db.init_db()
        runs = ai_eval.recent_runs(1)
        previous = runs[0]["headline"] if runs else None
    except Exception as exc:  # noqa: BLE001 - the report works without a database
        print("(no database: skipping comparison)", str(exc)[:80])
    regressions = ai_eval.compare(ai_eval.headline(results), previous)
    report = ai_eval.to_markdown(model, results, regressions)
    pathlib.Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    if args.save:
        print("saved run", ai_eval.save_run(model, results))
    return 0 if all(ai_eval.verdicts(results).values()) else 1


if __name__ == "__main__":
    sys.exit(main())
