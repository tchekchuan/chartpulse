# ============================================================
# File: ark_tracker.py
# Date: 2026-08-16
# Task: Tracks Cathie Wood / ARK Invest's inferred daily trades for
#       ARKK. ARK doesn't publish an explicit "trades" feed -- only a
#       daily holdings-snapshot CSV -- so trades are inferred by
#       diffing consecutive snapshots (same approach third-party
#       trackers like cathiesark.com use). Backed by the same Neon
#       Postgres used by subscribers.py, since Render's free tier has
#       no persistent disk (a local file wouldn't survive redeploys).
# ============================================================

import csv
import io
import os
from datetime import datetime

import psycopg2
import requests

DATABASE_URL = os.environ.get("DATABASE_URL")

# Confirmed live on ark-funds.com's own "Full Holdings CSV" link -- not a
# guessed URL. Only ARKK for now; ARK's other 5 ETFs likely follow the same
# assets.ark-funds.com/.../ARK_<NAME>_ETF_<TICKER>_HOLDINGS.csv pattern but
# each fund name needs verifying before trusting the URL.
ARKK_CSV_URL = "https://assets.ark-funds.com/fund-documents/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv"


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("ark_tracker: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ark_holdings (
                    fund          TEXT NOT NULL,
                    ticker        TEXT NOT NULL,
                    company       TEXT,
                    shares        BIGINT,
                    weight_pct    REAL,
                    snapshot_date DATE NOT NULL,
                    PRIMARY KEY (fund, ticker, snapshot_date)
                )
            """)
        conn.commit()
    print("ark_tracker: table ready")


def fetch_and_store(fund="ARKK", url=ARKK_CSV_URL):
    """Fetches today's holdings CSV and upserts it as a snapshot.
    Idempotent -- safe to call repeatedly the same day."""
    if not DATABASE_URL:
        return False

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))

    rows = []
    for row in reader:
        ticker = (row.get("ticker") or "").strip()
        if not ticker:
            continue
        try:
            shares = int((row.get("shares") or "0").replace(",", ""))
        except ValueError:
            continue
        weight_raw = (row.get("weight (%)") or "").replace("%", "").strip()
        try:
            weight_pct = float(weight_raw)
        except ValueError:
            weight_pct = None
        try:
            snap_date = datetime.strptime((row.get("date") or "").strip(), "%m/%d/%Y").date()
        except ValueError:
            snap_date = datetime.utcnow().date()
        rows.append((fund, ticker, row.get("company", ""), shares, weight_pct, snap_date))

    if not rows:
        return False

    with _conn() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT INTO ark_holdings (fund, ticker, company, shares, weight_pct, snapshot_date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (fund, ticker, snapshot_date) DO UPDATE SET
                        shares = EXCLUDED.shares, weight_pct = EXCLUDED.weight_pct, company = EXCLUDED.company
                """, r)
        conn.commit()
    return True


def get_latest_trades(fund="ARKK"):
    """Diffs the two most recent distinct snapshot dates on file to infer
    trades. Returns (trades, latest_date, previous_date) -- trades sorted
    by |share delta| descending. Empty list if fewer than 2 snapshots
    exist yet (e.g. the very first day this has ever run)."""
    if not DATABASE_URL:
        return [], None, None

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT snapshot_date FROM ark_holdings WHERE fund = %s "
                "ORDER BY snapshot_date DESC LIMIT 2",
                (fund,),
            )
            dates = [r[0] for r in cur.fetchall()]
            if len(dates) < 2:
                return [], (dates[0].isoformat() if dates else None), None

            latest_date, prev_date = dates[0], dates[1]
            cur.execute(
                "SELECT ticker, company, shares, weight_pct FROM ark_holdings "
                "WHERE fund = %s AND snapshot_date = %s",
                (fund, latest_date),
            )
            latest = {r[0]: {"company": r[1], "shares": r[2], "weight_pct": r[3]} for r in cur.fetchall()}

            cur.execute(
                "SELECT ticker, shares FROM ark_holdings WHERE fund = %s AND snapshot_date = %s",
                (fund, prev_date),
            )
            prev = {r[0]: r[1] for r in cur.fetchall()}

    trades = []
    for t in set(latest) | set(prev):
        new_shares = latest.get(t, {}).get("shares", 0)
        old_shares = prev.get(t, 0)
        delta = new_shares - old_shares
        if delta == 0:
            continue
        action = ("NEW POSITION" if old_shares == 0
                  else "EXITED" if new_shares == 0
                  else "BUY" if delta > 0 else "SELL")
        trades.append({
            "ticker":       t,
            "company":      latest.get(t, {}).get("company"),
            "shares_delta": delta,
            "pct_change":   round(delta / old_shares * 100, 1) if old_shares else None,
            "action":       action,
            "shares_now":   new_shares,
            "weight_pct":   latest.get(t, {}).get("weight_pct"),
        })

    trades.sort(key=lambda x: abs(x["shares_delta"]), reverse=True)
    return trades, latest_date.isoformat(), prev_date.isoformat()
