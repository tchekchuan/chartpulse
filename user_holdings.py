# ============================================================
# File: user_holdings.py
# Date: 2026-08-16
# Task: Per-subscriber portfolio holdings for the login-gated
#       "My Portfolio" view. Keyed by email (the same identity as
#       subscribers.py -- no separate user-id system). Backed by
#       Neon Postgres (Render's free tier has no persistent disk).
# ============================================================

import os

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")

MAX_HOLDINGS_PER_USER = 25  # sanity cap, not a hard product limit


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("user_holdings: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_holdings (
                    email    TEXT NOT NULL,
                    ticker   TEXT NOT NULL,
                    added_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (email, ticker)
                )
            """)
        conn.commit()
    print("user_holdings: table ready")


def get_holdings(email):
    if not DATABASE_URL:
        return []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ticker FROM user_holdings WHERE email = %s ORDER BY added_at",
                (email,),
            )
            return [r[0] for r in cur.fetchall()]


def add_holding(email, ticker):
    ticker = (ticker or "").strip().upper()
    if not DATABASE_URL or not ticker or len(ticker) > 12:
        return False, "Invalid ticker."
    current = get_holdings(email)
    if ticker in current:
        return True, f"{ticker} is already in your portfolio."
    if len(current) >= MAX_HOLDINGS_PER_USER:
        return False, f"Portfolio limit reached ({MAX_HOLDINGS_PER_USER} symbols max)."
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_holdings (email, ticker) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (email, ticker),
            )
        conn.commit()
    return True, f"Added {ticker}."


def remove_holding(email, ticker):
    ticker = (ticker or "").strip().upper()
    if not DATABASE_URL:
        return False
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_holdings WHERE email = %s AND ticker = %s", (email, ticker))
        conn.commit()
    return True
