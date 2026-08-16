# ============================================================
# File: track_record.py
# Date: 2026-08-16
# Task: Backtests getChartPulse's own signals against reality.
#       alerts.py logs a signal here the first time a symbol newly
#       reaches STRONG BUY, or newly crosses into a BUY zone on a
#       held position -- the same moments that already trigger a
#       real alert, so this is a forward-looking, non-cherry-picked
#       track record starting from whenever this first deployed.
#       Backed by Neon Postgres (Render's free tier has no
#       persistent disk).
# ============================================================

import os
from datetime import datetime

import psycopg2
import yfinance as yf

DATABASE_URL = os.environ.get("DATABASE_URL")

# Calendar days to wait for target/stop to be hit before forcing a
# resolution based on whichever side of entry the price ended up on.
RESOLUTION_WINDOW_DAYS = 30


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("track_record: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signal_track (
                    id            SERIAL PRIMARY KEY,
                    symbol        TEXT NOT NULL,
                    signal_date   DATE NOT NULL,
                    rating        TEXT NOT NULL,
                    score         INTEGER,
                    entry_price   REAL,
                    stop_price    REAL,
                    target_price  REAL,
                    outcome       TEXT NOT NULL DEFAULT 'PENDING',
                    outcome_date  DATE,
                    outcome_price REAL,
                    return_pct    REAL,
                    UNIQUE (symbol, signal_date, rating)
                )
            """)
        conn.commit()
    print("track_record: table ready")


def log_signal(symbol, rating, score, entry_price, stop_price, target_price, signal_date=None):
    """Records a new signal to backtest. Idempotent on (symbol, date,
    rating) -- safe even though alerts.py only calls this on a genuine
    state change, not every check cycle."""
    if not DATABASE_URL or not entry_price or not stop_price or not target_price:
        return
    signal_date = signal_date or datetime.utcnow().date()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_track
                    (symbol, signal_date, rating, score, entry_price, stop_price, target_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, signal_date, rating) DO NOTHING
            """, (symbol, signal_date, rating, score, entry_price, stop_price, target_price))
        conn.commit()


def _resolve_one(symbol, signal_date, entry, stop, target):
    """Walks daily price history since signal_date looking for target or
    stop hit first. Returns (outcome, outcome_date, outcome_price,
    return_pct), or None if still genuinely pending (within the window,
    neither hit yet)."""
    try:
        df = yf.Ticker(symbol).history(start=signal_date, interval="1d")
    except Exception:
        return None
    if df is None or df.empty:
        return None

    for idx, row in df.iterrows():
        day = idx.date()
        hi, lo = float(row["High"]), float(row["Low"])
        hit_target = hi >= target
        hit_stop   = lo <= stop
        if hit_stop:
            # Conservative convention: if both target and stop were touched
            # the same day, assume the stop was hit first intraday -- daily
            # bars can't tell us the true order.
            return "LOSS", day, stop, round((stop - entry) / entry * 100, 2)
        if hit_target:
            return "WIN", day, target, round((target - entry) / entry * 100, 2)

    age_days = (datetime.utcnow().date() - signal_date).days
    if age_days >= RESOLUTION_WINDOW_DAYS:
        last_close = float(df["Close"].iloc[-1])
        last_date  = df.index[-1].date()
        outcome = "EXPIRED_WIN" if last_close > entry else "EXPIRED_LOSS"
        return outcome, last_date, last_close, round((last_close - entry) / entry * 100, 2)

    return None


def resolve_pending():
    """Checks every PENDING signal and resolves any that have hit
    target/stop or aged past the resolution window. Cheap to call on
    every alert cycle -- most calls resolve zero rows."""
    if not DATABASE_URL:
        return 0
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, symbol, signal_date, entry_price, stop_price, target_price "
                "FROM signal_track WHERE outcome = 'PENDING'"
            )
            pending = cur.fetchall()

    resolved = 0
    for id_, symbol, signal_date, entry, stop, target in pending:
        result = _resolve_one(symbol, signal_date, entry, stop, target)
        if result is None:
            continue
        outcome, outcome_date, outcome_price, return_pct = result
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE signal_track SET outcome=%s, outcome_date=%s, "
                    "outcome_price=%s, return_pct=%s WHERE id=%s",
                    (outcome, outcome_date, outcome_price, return_pct, id_),
                )
            conn.commit()
        resolved += 1
    return resolved


def get_track_record():
    """Aggregate win-rate stats plus the most recent signals, newest first."""
    empty = {"total": 0, "resolved": 0, "pending": 0, "win_rate": None,
              "by_rating": {}, "recent": []}
    if not DATABASE_URL:
        return empty

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, signal_date, rating, score, entry_price, stop_price, "
                "target_price, outcome, outcome_date, outcome_price, return_pct "
                "FROM signal_track ORDER BY signal_date DESC LIMIT 200"
            )
            rows = cur.fetchall()

    cols = ["symbol", "signal_date", "rating", "score", "entry_price", "stop_price",
            "target_price", "outcome", "outcome_date", "outcome_price", "return_pct"]
    all_rows = [dict(zip(cols, r)) for r in rows]

    resolved = [r for r in all_rows if r["outcome"] != "PENDING"]
    wins     = [r for r in resolved if r["outcome"] in ("WIN", "EXPIRED_WIN")]
    win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else None

    by_rating = {}
    for rating in ("STRONG BUY", "BUY"):
        rr = [r for r in resolved if r["rating"] == rating]
        rw = [r for r in rr if r["outcome"] in ("WIN", "EXPIRED_WIN")]
        by_rating[rating] = {
            "resolved":       len(rr),
            "win_rate":       round(len(rw) / len(rr) * 100, 1) if rr else None,
            "avg_return_pct": round(sum(r["return_pct"] for r in rr) / len(rr), 2) if rr else None,
        }

    for r in all_rows:
        r["signal_date"]  = r["signal_date"].isoformat()  if r["signal_date"]  else None
        r["outcome_date"] = r["outcome_date"].isoformat() if r["outcome_date"] else None

    return {
        "total":     len(all_rows),
        "resolved":  len(resolved),
        "pending":   len(all_rows) - len(resolved),
        "win_rate":  win_rate,
        "by_rating": by_rating,
        "recent":    all_rows[:50],
    }
