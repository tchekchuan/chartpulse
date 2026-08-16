# ============================================================
# File: user_watchlist.py
# Date: 2026-08-16
# Task: Per-subscriber watchlist symbols -- research/watching, not
#       held. Mirrors user_holdings.py (My Portfolio) but with
#       different alert semantics: watchlist symbols only trigger an
#       alert on a new STRONG BUY (matching how Shawn's own
#       WATCHLIST vs PORTFOLIO already behave in alerts.py), never
#       a SELL alert, since you can't sell what you don't hold.
#       Backed by Neon Postgres (Render's free tier has no
#       persistent disk).
# ============================================================

import os

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")

MAX_WATCHLIST_PER_USER = 25  # sanity cap, not a hard product limit


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("user_watchlist: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_watchlist (
                    email    TEXT NOT NULL,
                    ticker   TEXT NOT NULL,
                    added_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (email, ticker)
                )
            """)
        conn.commit()
    print("user_watchlist: table ready")


def get_watchlist(email):
    if not DATABASE_URL:
        return []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ticker FROM user_watchlist WHERE email = %s ORDER BY added_at",
                (email,),
            )
            return [r[0] for r in cur.fetchall()]


def add_watch(email, ticker):
    ticker = (ticker or "").strip().upper()
    if not DATABASE_URL or not ticker or len(ticker) > 12:
        return False, "Invalid ticker."
    current = get_watchlist(email)
    if ticker in current:
        return True, f"{ticker} is already on your watchlist."
    if len(current) >= MAX_WATCHLIST_PER_USER:
        return False, f"Watchlist limit reached ({MAX_WATCHLIST_PER_USER} symbols max)."
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_watchlist (email, ticker) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (email, ticker),
            )
        conn.commit()
    return True, f"Added {ticker}."


def remove_watch(email, ticker):
    ticker = (ticker or "").strip().upper()
    if not DATABASE_URL:
        return False
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_watchlist WHERE email = %s AND ticker = %s", (email, ticker))
        conn.commit()
    return True


def get_all_watchlist_by_symbol():
    """Returns {ticker: [email, email, ...]} across every subscriber --
    the reverse index alerts.py needs to fan out STRONG BUY notices to
    whoever is watching that symbol."""
    if not DATABASE_URL:
        return {}
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, email FROM user_watchlist")
            rows = cur.fetchall()
    result = {}
    for ticker, email in rows:
        result.setdefault(ticker, []).append(email)
    return result
