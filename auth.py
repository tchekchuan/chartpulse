# ============================================================
# File: auth.py
# Date: 2026-08-16
# Task: Magic-link login for confirmed subscribers, so they can
#       manage their own "My Portfolio". Reuses the same
#       email-click-to-verify pattern as subscribers.py's confirm
#       flow -- no passwords, no OAuth, no new external service.
#       Backed by Neon Postgres (Render's free tier has no
#       persistent disk).
# ============================================================

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import psycopg2

from mailer import send_email as _send_email

DATABASE_URL = os.environ.get("DATABASE_URL")
SITE_URL = "https://getchartpulse.com"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LOGIN_TOKEN_TTL_MINUTES = 15
GENERIC_MSG = "If that email is a confirmed subscriber, a login link is on its way."


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("auth: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS login_tokens (
                    token      TEXT PRIMARY KEY,
                    email      TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used       BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
        conn.commit()
    print("auth: table ready")


def request_login_link(email):
    """Sends a magic login link only if the email is a confirmed
    subscriber. Always returns the same generic message either way, so
    this endpoint can't be used to enumerate who's subscribed."""
    email = (email or "").strip().lower()
    if not DATABASE_URL or not _EMAIL_RE.match(email):
        return GENERIC_MSG

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT confirmed FROM subscribers WHERE email = %s", (email,))
            row = cur.fetchone()
    if not row or not row[0]:
        return GENERIC_MSG  # not a confirmed subscriber -- silently no-op

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=LOGIN_TOKEN_TTL_MINUTES)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO login_tokens (token, email, expires_at) VALUES (%s, %s, %s)",
                (token, email, expires_at),
            )
        conn.commit()

    login_url = f"{SITE_URL}/api/auth/verify?token={token}"
    _send_email(
        email, "Your getChartPulse login link",
        f"Click to log in (expires in {LOGIN_TOKEN_TTL_MINUTES} minutes):\n\n{login_url}\n\n"
        f"If you didn't request this, ignore this email.",
    )
    return GENERIC_MSG


def verify_login_token(token):
    """Verifies a login token and returns its email if valid and
    unexpired, or None. Deliberately NOT single-use within the TTL --
    mail clients (iOS Mail, Gmail, etc.) commonly prefetch/scan links in
    incoming email before a human ever clicks them, same issue already
    hit and fixed for the subscribe-confirm flow. A strictly single-use
    token would mean the real user's own click almost always fails
    because their mail client already burned it. `used`/`used_at` are
    still recorded, purely for visibility -- they don't gate anything."""
    if not DATABASE_URL or not token:
        return None
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email, expires_at FROM login_tokens WHERE token = %s",
                (token,),
            )
            row = cur.fetchone()
            if not row:
                return None
            email, expires_at = row
            if expires_at < datetime.now(timezone.utc):
                return None
            cur.execute("UPDATE login_tokens SET used = TRUE WHERE token = %s", (token,))
        conn.commit()
    return email
