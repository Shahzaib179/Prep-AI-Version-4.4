"""Reminder settings (student): opt in to WhatsApp reminders, choose the time, preview, or send yourself a WhatsApp link."""
from __future__ import annotations

import streamlit as st

import reminders


def render_reminders(student_id: str, name: str) -> None:
    prefs = reminders.get_prefs(student_id)
    with st.expander("🔔 WhatsApp reminders" + (" (on)" if prefs["enabled"] else "")):
        st.caption("One short message a day, only when it helps: a streak about to end, an assignment due soon, revision due, or the Daily Challenge. You can turn it off any time here.")
        with st.form("reminder_form"):
            phone = st.text_input("Your WhatsApp number", value=prefs["phone"], placeholder="0300 1234567 or +92 300 1234567")
            c1, c2 = st.columns(2)
            hour = c1.selectbox("Remind me around", list(range(6, 23)), index=max(0, min(16, prefs["hour_local"] - 6)), format_func=lambda h: f"{(h - 1) % 12 + 1}:00 {'AM' if h < 12 else 'PM'}")
            kinds = c2.multiselect("Remind me about", list(reminders.KINDS), default=prefs["kinds"] or list(reminders.KINDS),
                                   format_func=lambda k: {"streak": "Streak / practice", "revision": "Revision due", "assignment": "Assignments", "daily": "Daily Challenge"}[k])
            enabled = st.checkbox("Send me WhatsApp reminders", value=prefs["enabled"])
            consent = st.checkbox("I agree that Prep AI may message this WhatsApp number with study reminders.", value=bool(prefs["consented_at"]))
            if st.form_submit_button("Save", type="primary"):
                ok, msg = reminders.save_prefs(student_id, phone, enabled, hour, kinds, consent)
                (st.success if ok else st.error)(msg)
        if st.button("Preview my reminder", key="rem_preview"):
            preview = reminders.compose(student_id, prefs["kinds"] or reminders.KINDS, name)
            if preview:
                st.code(preview, language=None)
                st.link_button("Send this to myself on WhatsApp", reminders.wa_link(preview, prefs["phone"] or None))
            else:
                st.success("Nothing to remind you about right now. You are all caught up.")
        st.caption("The preview button opens WhatsApp with the text ready; it always works. Automatic daily sending is run by the app owner's scheduled job.")
