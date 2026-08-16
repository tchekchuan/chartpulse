# ============================================================
# File: subscribers.py
# Date: 2026-08-15
# Task: Public email subscriber list for getChartPulse STRONG BUY
#       alerts. Backed by Neon (Postgres) since Render's free tier
#       has no persistent disk -- local files get wiped on redeploy.
# ============================================================

import os
import re
import secrets

import psycopg2

from mailer import send_email as _send_email
from telegram_notify import send_telegram

DATABASE_URL = os.environ.get("DATABASE_URL")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SITE_URL = "https://getchartpulse.com"


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("subscribers: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    token TEXT NOT NULL,
                    confirmed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
        conn.commit()
    print("subscribers: table ready")


def subscribe(email):
    """Adds an email (unconfirmed) and sends a confirmation link.
    Returns (ok: bool, message: str)."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return False, "That doesn't look like a valid email address."
    if not DATABASE_URL:
        return False, "Subscriptions are temporarily unavailable."

    token = secrets.token_urlsafe(24)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT confirmed FROM subscribers WHERE email = %s", (email,))
            row = cur.fetchone()
            if row and row[0]:
                return True, "You're already subscribed."
            if row:
                cur.execute("UPDATE subscribers SET token = %s WHERE email = %s", (token, email))
            else:
                cur.execute(
                    "INSERT INTO subscribers (email, token) VALUES (%s, %s)",
                    (email, token),
                )
        conn.commit()

    confirm_url = f"{SITE_URL}/api/subscribe/confirm?token={token}"
    _send_email(
        email,
        "Confirm your getChartPulse alerts",
        f"Click to confirm you want STRONG BUY signal alerts from getChartPulse:\n\n{confirm_url}\n\n"
        f"If you didn't request this, ignore this email.",
    )
    return True, "Check your email to confirm your subscription."


def confirm(token):
    """Marks a subscription confirmed and returns the email, or None if the
    token doesn't exist. Sends the thank-you email only the first time --
    mail clients/security scanners often prefetch links in incoming email,
    which would otherwise hit this route (and re-send the email) before the
    user ever clicks it themselves."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT confirmed FROM subscribers WHERE token = %s", (token,))
            existing = cur.fetchone()
            if not existing:
                return None
            already_confirmed = existing[0]
            cur.execute("UPDATE subscribers SET confirmed = TRUE WHERE token = %s RETURNING email", (token,))
            row = cur.fetchone()
        conn.commit()
    email = row[0]
    if not already_confirmed:
        unsub_url = f"{SITE_URL}/api/subscribe/unsubscribe?token={token}"
        _send_email(
            email,
            "You're subscribed to getChartPulse alerts",
            f"Thanks for subscribing! You'll get an email whenever a symbol on getChartPulse's "
            f"watchlist hits a STRONG BUY rating.\n\n"
            f"Not financial advice -- always do your own research.\n\n"
            f"---\nUnsubscribe anytime: {unsub_url}",
        )
        send_telegram(f"🎉 *New getChartPulse subscriber*\n{email}")
    return email


def confirm_email(email):
    """Ensures email is a confirmed subscriber, creating the row if needed.
    Used by the login flow (auth.py): successfully verifying a login
    link/code is itself proof of email ownership, equivalent to the
    double opt-in click, so a first-time login can subscribe someone in
    one step instead of requiring a separate Subscribe-then-confirm
    flow first. Returns True if this call just newly confirmed them (so
    the caller knows to send the welcome email), False if already
    confirmed or on invalid input."""
    email = (email or "").strip().lower()
    if not DATABASE_URL or not _EMAIL_RE.match(email):
        return False

    new_token = secrets.token_urlsafe(24)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT confirmed, token FROM subscribers WHERE email = %s", (email,))
            row = cur.fetchone()
            if row and row[0]:
                return False  # already confirmed, nothing to do
            if row:
                cur.execute("UPDATE subscribers SET confirmed = TRUE WHERE email = %s", (email,))
                token = row[1]
            else:
                cur.execute(
                    "INSERT INTO subscribers (email, token, confirmed) VALUES (%s, %s, TRUE)",
                    (email, new_token),
                )
                token = new_token
        conn.commit()

    unsub_url = f"{SITE_URL}/api/subscribe/unsubscribe?token={token}"
    _send_email(
        email,
        "You're subscribed to getChartPulse alerts",
        f"Logging in also subscribes you to getChartPulse alerts -- you'll get an email "
        f"whenever a symbol on your My Portfolio or My Watchlist changes, plus the public "
        f"STRONG BUY signal broadcast.\n\n"
        f"Not financial advice -- always do your own research.\n\n"
        f"---\nUnsubscribe anytime: {unsub_url}",
    )
    send_telegram(f"🎉 *New getChartPulse subscriber* (via login)\n{email}")
    return True


def unsubscribe(token):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscribers WHERE token = %s RETURNING email", (token,))
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def get_confirmed_subscribers():
    """Returns [(email, token), ...] for all confirmed subscribers."""
    if not DATABASE_URL:
        return []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email, token FROM subscribers WHERE confirmed = TRUE")
            return cur.fetchall()


def get_all_subscribers():
    """Returns [(email, confirmed, created_at), ...] for every row, newest first."""
    if not DATABASE_URL:
        return []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email, confirmed, created_at FROM subscribers ORDER BY created_at DESC"
            )
            return cur.fetchall()


def send_strong_buy_alert(lines_text):
    """Emails all confirmed subscribers the STRONG BUY summary."""
    subs = get_confirmed_subscribers()
    if not subs:
        return 0
    sent = 0
    for email, token in subs:
        unsub_url = f"{SITE_URL}/api/subscribe/unsubscribe?token={token}"
        body = f"{lines_text}\n\n---\nUnsubscribe: {unsub_url}"
        if _send_email(email, "getChartPulse: STRONG BUY signal", body):
            sent += 1
    return sent
