"""WhatsApp reminders (opt-in): one short message per day, only when it is useful, sent by a scheduled job.

Free ways to deliver (see docs/V4.7_MONTH2.md):
  * dry_run   - writes the message to the log only (default; use it to test)
  * twilio    - Twilio WhatsApp (free sandbox for testing; a paid approved sender for real use)
  * cloudapi  - Meta WhatsApp Cloud API (needs an approved message template for messages you start)
  * link      - nothing is sent by the server; the app shows a button that opens WhatsApp with the text ready (always free)
No new Python packages: HTTP is done with urllib. Pure logic (no Streamlit).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable

import config
import progress
from db_core import connect

KINDS = ("streak", "revision", "assignment", "daily")
DEFAULT_COUNTRY = "92"          # numbers written as 03001234567 are treated as Pakistan
LAST_HOUR = 22                  # never send later than 22:00 local time
MAX_FAILED_TRIES = 2


def normalize_phone(raw: str, default_country: str = DEFAULT_COUNTRY) -> str | None:
    """'0300 1234567' / '+92 300 1234567' / '0092300...' -> '+923001234567'; None if it cannot be a phone number."""
    s = re.sub(r"[\s\-().]", "", str(raw or ""))
    if not s:
        return None
    if s.startswith("+"):
        digits = s[1:]
    elif s.startswith("00"):
        digits = s[2:]
    elif s.startswith("0"):
        digits = default_country + s[1:]
    else:
        digits = s
    if not digits.isdigit() or not (10 <= len(digits) <= 15) or digits.startswith("0"):
        return None
    return "+" + digits


def mask_phone(phone: str | None) -> str:
    return (phone[:4] + "•" * max(0, len(phone) - 7) + phone[-3:]) if phone and len(phone) > 7 else (phone or "")


# ------------------------------------------------------------------ preferences
def get_prefs(student_id: str) -> dict[str, Any]:
    with connect() as con:
        r = con.execute("SELECT phone,enabled,hour_local,kinds,consented_at FROM reminder_prefs WHERE student_id=?", (student_id,)).fetchone()
    base = {"phone": "", "enabled": False, "hour_local": 19, "kinds": list(KINDS), "consented_at": None}
    if not r:
        return base
    return {"phone": r["phone"] or "", "enabled": bool(r["enabled"]), "hour_local": int(r["hour_local"] if r["hour_local"] is not None else 19),
            "kinds": [k for k in (r["kinds"] or "").split(",") if k in KINDS], "consented_at": r["consented_at"]}


def save_prefs(student_id: str, phone: str, enabled: bool, hour_local: int, kinds: list[str], consent: bool) -> tuple[bool, str]:
    """Turning reminders on needs a valid number AND the student's explicit consent."""
    phone_n = normalize_phone(phone) if phone else None
    if enabled:
        if not phone_n:
            return False, "Enter a valid WhatsApp number, for example 0300 1234567 or +92 300 1234567."
        if not consent:
            return False, "Please tick the box to confirm you want WhatsApp reminders on this number."
    elif phone and not phone_n:
        return False, "That number does not look valid."
    hour = max(6, min(LAST_HOUR, int(hour_local)))
    now = datetime.utcnow().isoformat()
    kinds_s = ",".join(k for k in kinds if k in KINDS)
    with connect() as con:
        old = con.execute("SELECT consented_at FROM reminder_prefs WHERE student_id=?", (student_id,)).fetchone()
        consented = (old["consented_at"] if old and old["consented_at"] else None) if not (enabled and consent) else (old["consented_at"] if old and old["consented_at"] else now)
        con.execute("INSERT INTO reminder_prefs(student_id,phone,enabled,hour_local,consented_at,kinds,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(student_id) DO UPDATE SET phone=excluded.phone, enabled=excluded.enabled, hour_local=excluded.hour_local, consented_at=excluded.consented_at, kinds=excluded.kinds, updated_at=excluded.updated_at",
                    (student_id, phone_n or "", int(bool(enabled)), hour, consented, kinds_s, now))
    return True, "Saved." if enabled else "Reminders are off."


def disable(student_id: str) -> None:
    with connect() as con:
        con.execute("UPDATE reminder_prefs SET enabled=0, updated_at=? WHERE student_id=?", (datetime.utcnow().isoformat(), student_id))


# ------------------------------------------------------------------ message
def compose(student_id: str, kinds: list[str] | tuple[str, ...] = KINDS, name: str = "") -> str | None:
    """One short, useful message, or None when there is nothing worth saying (e.g. the student already practised and nothing is due)."""
    import classes
    import daily
    import db
    today = progress.local_today()
    p = progress.get_progress(student_id, today)
    lines: list[str] = []
    if "streak" in kinds and p["streak"] > 0 and p["today_questions"] == 0:
        lines.append(f"🔥 Your {p['streak']}-day streak ends tonight. One quiz keeps it alive.")
    elif "streak" in kinds and p["today_questions"] == 0 and not p["goal_done"]:
        lines.append("🎯 You have not practised yet today. Even 10 questions count.")
    if "assignment" in kinds:
        due = [a for a in classes.student_assignments(student_id) if a["status"] in ("due_soon", "overdue")]
        for a in due[:2]:
            lines.append(f"📌 Assignment “{a['title']}” is due {classes.fmt_local(a['due_at'])}.")
    if "revision" in kinds:
        n = len(db.due_revisions(student_id))
        if n:
            lines.append(f"🔄 {n} topic{'s' if n != 1 else ''} due for revision today.")
    if "daily" in kinds and daily.result_for(student_id, today) is None:
        lines.append("⚡ Today's 5-question Daily Challenge is waiting.")
    if not lines:
        return None
    greeting = f"Hi {name.split()[0]}, " if name.strip() else ""
    return f"Prep AI: {greeting}quick reminder\n" + "\n".join(lines)


def wa_link(text: str, phone: str | None = None) -> str:
    """wa.me link that opens WhatsApp with the text ready to send (free, needs the student to press send)."""
    base = f"https://wa.me/{phone.lstrip('+')}" if phone else "https://wa.me/"
    return base + "?text=" + urllib.parse.quote(text)


# ------------------------------------------------------------------ transports
def _http(url: str, data: bytes, headers: dict[str, str], timeout: float = 15.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]


def _env(name: str) -> str:
    return (os.environ.get(name) or str(config.get_secret(name, "") or "")).strip()


def send_message(provider: str, phone: str, text: str, http: Callable[..., tuple[int, str]] = _http) -> tuple[bool, str]:
    provider = (provider or "dry_run").lower()
    if provider == "dry_run":
        return True, "dry run (nothing sent)"
    if provider == "twilio":
        sid, token, sender = _env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN"), _env("TWILIO_WHATSAPP_FROM")
        if not (sid and token and sender):
            return False, "Twilio settings are missing (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM)."
        body = urllib.parse.urlencode({"From": f"whatsapp:{sender}", "To": f"whatsapp:{phone}", "Body": text}).encode()
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        code, resp = http(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", body, {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
        return (200 <= code < 300), f"HTTP {code}" + ("" if 200 <= code < 300 else f": {resp}")
    if provider == "cloudapi":
        token, phone_id, template = _env("WA_CLOUD_TOKEN"), _env("WA_PHONE_NUMBER_ID"), _env("WA_TEMPLATE_NAME")
        if not (token and phone_id):
            return False, "Cloud API settings are missing (WA_CLOUD_TOKEN, WA_PHONE_NUMBER_ID)."
        if template:   # business-started messages must use an approved template with ONE body variable
            payload = {"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "template",
                       "template": {"name": template, "language": {"code": _env("WA_TEMPLATE_LANG") or "en"},
                                    "components": [{"type": "body", "parameters": [{"type": "text", "text": text.replace("\n", " | ")[:900]}]}]}}
        else:
            payload = {"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "text", "text": {"body": text}}
        code, resp = http(f"https://graph.facebook.com/v21.0/{phone_id}/messages", json.dumps(payload).encode(), {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        return (200 <= code < 300), f"HTTP {code}" + ("" if 200 <= code < 300 else f": {resp}")
    return False, f"Unknown provider '{provider}'."


# ------------------------------------------------------------------ the scheduled job
def _local(now_utc: datetime) -> datetime:
    return now_utc + timedelta(minutes=config.PROGRESS_TZ_OFFSET_MIN)


def due_students(now_utc: datetime | None = None) -> list[dict[str, Any]]:
    """Students who opted in, whose reminder hour has come (and not yet passed 22:00), with no digest sent or twice failed today."""
    loc = _local(now_utc or datetime.utcnow())
    day = loc.date().isoformat()
    if loc.hour > LAST_HOUR:
        return []
    with connect() as con:
        rows = con.execute("SELECT p.student_id,p.phone,p.hour_local,p.kinds,COALESCE(s.name,'') AS name FROM reminder_prefs p LEFT JOIN students s ON s.id=p.student_id "
                           "WHERE p.enabled=1 AND p.phone<>'' AND p.hour_local<=?", (loc.hour,)).fetchall()
        out = []
        for r in rows:
            sent = con.execute("SELECT COUNT(*) FROM reminder_log WHERE student_id=? AND day=? AND status IN ('sent','skipped')", (r["student_id"], day)).fetchone()[0]
            failed = con.execute("SELECT COUNT(*) FROM reminder_log WHERE student_id=? AND day=? AND status='failed'", (r["student_id"], day)).fetchone()[0]
            if sent == 0 and failed < MAX_FAILED_TRIES:
                out.append({**dict(r), "kinds": [k for k in (r["kinds"] or "").split(",") if k in KINDS]})
    return out


def _log(student_id: str, day: str, status: str, detail: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO reminder_log(student_id,day,kind,status,detail,sent_at) VALUES(?,?,?,?,?,?)", (student_id, day, "digest", status, detail[:300], datetime.utcnow().isoformat()))


def run_due(provider: str = "dry_run", now_utc: datetime | None = None, http: Callable[..., tuple[int, str]] = _http, limit: int = 500) -> dict[str, Any]:
    """Send today's reminders to everyone who is due. Safe to run every hour: each student gets at most one message per day."""
    now_utc = now_utc or datetime.utcnow()
    day = _local(now_utc).date().isoformat()
    summary = {"day": day, "candidates": 0, "sent": 0, "skipped": 0, "failed": 0}
    for s in due_students(now_utc)[:limit]:
        summary["candidates"] += 1
        try:
            text = compose(s["student_id"], s["kinds"], s["name"])
        except Exception as exc:  # noqa: BLE001 - one bad student must not stop the run
            _log(s["student_id"], day, "failed", f"compose error: {exc}")
            summary["failed"] += 1
            continue
        if not text:
            _log(s["student_id"], day, "skipped", "nothing to remind about")
            summary["skipped"] += 1
            continue
        ok, detail = send_message(provider, s["phone"], text, http)
        _log(s["student_id"], day, "sent" if ok else "failed", detail)
        summary["sent" if ok else "failed"] += 1
    return summary
