"""Send today's WhatsApp reminders. Run it every hour (GitHub Actions does this for free, see .github/workflows/reminders.yml).

Usage:  DATABASE_URL=postgresql://...  REMINDER_PROVIDER=dry_run|twilio|cloudapi  python scripts/send_reminders.py
Each opted-in student gets at most one message a day, after their chosen hour and never after 22:00 local time.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db  # noqa: E402  (also applies database migrations)
import reminders  # noqa: E402


def main() -> int:
    db.init_db()
    provider = os.environ.get("REMINDER_PROVIDER", "dry_run")
    summary = reminders.run_due(provider)
    print(f"provider={provider} {summary}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
