# ============================================================
# File: symbol_state.py
# Date: 2026-08-16
# Task: Persists the last-known rating/action per symbol, so
#       subscriber-portfolio alerts (alerts.py) can detect a genuine
#       BUY/SELL zone change instead of re-alerting every check.
#       Backed by Neon Postgres -- unlike the fixed watchlist's
#       alert_state.json (local disk, wiped on redeploy, an accepted
#       quirk since that only affects Shawn's own alerts), a spurious
#       re-alert here after a redeploy would spam real subscribers
#       about holdings they already know about.
# ============================================================

import os

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("symbol_state: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS symbol_state (
                    symbol       TEXT PRIMARY KEY,
                    rating       TEXT,
                    action       TEXT,
                    last_checked TIMESTAMPTZ DEFAULT now()
                )
            """)
        conn.commit()
    print("symbol_state: table ready")


def get_all():
    """Returns {symbol: {"rating": ..., "action": ...}}."""
    if not DATABASE_URL:
        return {}
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, rating, action FROM symbol_state")
            return {s: {"rating": r, "action": a} for s, r, a in cur.fetchall()}


def set_many(updates):
    """updates: {symbol: {"rating": ..., "action": ...}}"""
    if not DATABASE_URL or not updates:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            for symbol, v in updates.items():
                cur.execute("""
                    INSERT INTO symbol_state (symbol, rating, action, last_checked)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (symbol) DO UPDATE SET
                        rating = EXCLUDED.rating, action = EXCLUDED.action, last_checked = now()
                """, (symbol, v["rating"], v["action"]))
        conn.commit()
