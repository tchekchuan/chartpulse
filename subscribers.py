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
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE subscribers SET confirmed = TRUE WHERE token = %s RETURNING email", (token,))
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


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
