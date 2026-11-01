# ============================================================
# File: track_record.py
# Date: 2026-08-16 (corrected method 2026-11-02)
# Task: Backtests getChartPulse's own signals against reality.
#       alerts.py logs a signal here the first time a symbol newly
#       reaches STRONG BUY, or newly crosses into a BUY zone on a
#       held position -- the same moments a real alert fires.
#       Backed by Neon Postgres (Render's free tier has no disk).
#
# CORRECTED METHOD (2026-11-02). The original resolver overstated
# results in three verified ways:
#   1. Look-ahead: it read bars from history(start=signal_date), which
#      includes the signal day itself and, through time zones, sometimes
#      the day before -- 11 of 44 outcomes were dated BEFORE the signal.
#   2. No fill check: WIN when High >= target even if price never traded
#      down to the buy-zone entry, so un-enterable trades counted.
#   3. Repeats: a symbol re-signalled while an earlier signal was still
#      open was scored as another independent trade.
#
# What the corrected method does (resolve_v2 + recompute_all):
#   - Entry = OPEN of the first daily bar dated after the signal's UTC
#     date (2 days after for .AX / .NZ, whose sessions open before
#     midnight UTC). Every such session starts after the whole UTC day
#     the alert fired on, so no price before the alert can be used,
#     whatever hour the alert ran (alerts can also be triggered by hand,
#     so the hour is not known for old rows). Cost: the entry is
#     sometimes a day later than an alert-follower could have bought.
#   - If that open is already at/below the stop or at/above the target,
#     the setup no longer exists: SKIPPED, not counted as a trade.
#   - Then each daily bar in order, using the stored stop and target:
#       entry bar:   Low <= stop  -> LOSS at stop (even if High also
#                    reached target: daily bars can't show which came
#                    first, so assume the worse);  High >= target -> WIN.
#       later bars:  Open <= stop -> LOSS at the open (gapped through);
#                    Low <= stop -> LOSS at stop (again, the worse case if
#                    target was also touched); Open >= target -> WIN at
#                    the open; High >= target -> WIN at target.
#   - Not resolved within RESOLUTION_WINDOW_DAYS calendar days of the
#     signal -> EXPIRED at the last close in the window.
#   - One trade per symbol at a time: a signal on a symbol whose earlier
#     counted trade had not exited by that signal date is DUPLICATE and
#     excluded from the statistics (kept in the table, never deleted).
#   - Returns are before fees, slippage and tax.
# The original columns (outcome, return_pct, ...) are left untouched as
# the legacy record; the corrected results live in the v2_* columns and
# are recomputed from price data every cycle, so they are deterministic.
# ============================================================

import os
from datetime import date, datetime, timedelta, timezone

import psycopg2
import yfinance as yf

DATABASE_URL = os.environ.get("DATABASE_URL")

# Calendar days to wait for target/stop before closing at the last close.
RESOLUTION_WINDOW_DAYS = 30
# Below this many counted trades, the page says no conclusion can be drawn.
MIN_TRADES_FOR_CONCLUSION = 30
# Exchanges whose regular session opens before 00:00 UTC (verified: BHP.AX
# opens 23:00 UTC the previous UTC day). Their entry waits one more day.
EARLY_OPEN_SUFFIXES = (".AX", ".NZ")


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
            # Corrected-method columns (added 2026-11-02). Additive only.
            for col, typ in (("signal_at", "TIMESTAMPTZ"), ("price_at_signal", "REAL"),
                             ("rr_at_signal", "REAL"), ("entry_ok", "BOOLEAN"),
                             ("v2_status", "TEXT"), ("v2_entry_date", "DATE"), ("v2_entry", "REAL"),
                             ("v2_rr", "REAL"), ("v2_outcome", "TEXT"), ("v2_outcome_date", "DATE"),
                             ("v2_outcome_price", "REAL"), ("v2_return_pct", "REAL"),
                             ("v2_note", "TEXT"), ("v2_dup_of", "INTEGER")):
                cur.execute(f"ALTER TABLE signal_track ADD COLUMN IF NOT EXISTS {col} {typ}")
        conn.commit()
    print("track_record: table ready")


def log_signal(symbol, rating, score, entry_price, stop_price, target_price, signal_date=None,
               price_at_signal=None, rr_at_signal=None, entry_ok=None):
    """Records a new signal. Idempotent on (symbol, date, rating). Every
    buy-side signal is logged -- including ones whose entry was too poor to
    alert on -- so the corrected record can show whether the entry-quality
    gate actually helps, instead of only ever measuring what was alerted."""
    if not DATABASE_URL or not entry_price or not stop_price or not target_price:
        return
    now = datetime.now(timezone.utc)
    signal_date = signal_date or now.date()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_track
                    (symbol, signal_date, rating, score, entry_price, stop_price, target_price,
                     signal_at, price_at_signal, rr_at_signal, entry_ok)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, signal_date, rating) DO NOTHING
            """, (symbol, signal_date, rating, score, entry_price, stop_price, target_price,
                  now, price_at_signal, rr_at_signal, entry_ok))
        conn.commit()


# ── Corrected resolution: pure function, no I/O (unit-tested) ─────────────
def entry_min_date(symbol, signal_date):
    extra = 2 if symbol.upper().endswith(EARLY_OPEN_SUFFIXES) else 1
    return signal_date + timedelta(days=extra)


def resolve_v2(symbol, signal_date, stop, target, bars, today):
    """bars: [(date, open, high, low, close), ...] ascending, dates in the
    exchange's own calendar (yfinance daily index). Returns a dict with
    status in PENDING | SKIPPED | RESOLVED and the trade details."""
    start = entry_min_date(symbol, signal_date)
    window_end = signal_date + timedelta(days=RESOLUTION_WINDOW_DAYS)
    window = [b for b in bars if start <= b[0] <= window_end]
    if not window:
        if today > window_end:
            return {"status": "SKIPPED", "note": "no price data after the signal"}
        return {"status": "PENDING", "note": "waiting for the first session after the signal"}

    d0, o0, h0, l0, c0 = window[0]
    if o0 <= stop:
        return {"status": "SKIPPED", "entry_date": d0, "entry": o0,
                "note": f"opened at {o0:.2f}, at or below the stop {stop:.2f} -- no valid entry"}
    if o0 >= target:
        return {"status": "SKIPPED", "entry_date": d0, "entry": o0,
                "note": f"opened at {o0:.2f}, at or above the target {target:.2f} -- no valid entry"}
    entry = o0
    rr = (target - entry) / (entry - stop)

    def done(outcome, d, px, note):
        return {"status": "RESOLVED", "entry_date": d0, "entry": entry, "rr": rr, "outcome": outcome,
                "outcome_date": d, "outcome_price": px, "return_pct": (px / entry - 1) * 100, "note": note}

    for k, (d, o, h, l, c) in enumerate(window):
        if k == 0:
            if l <= stop:
                return done("LOSS", d, stop, "stop hit on the entry day" +
                            (" (target also touched that day; counted as the worse case)" if h >= target else ""))
            if h >= target:
                return done("WIN", d, target, "target hit on the entry day")
            continue
        if o <= stop:
            return done("LOSS", d, o, "opened below the stop (gap), exit at the open")
        if l <= stop:
            return done("LOSS", d, stop, "stop hit" +
                        (" (target also touched that day; counted as the worse case)" if h >= target else ""))
        if o >= target:
            return done("WIN", d, o, "opened above the target (gap), exit at the open")
        if h >= target:
            return done("WIN", d, target, "target hit")

    if today > window_end:
        d, c = window[-1][0], window[-1][4]
        return done("EXPIRED", d, c, f"neither hit within {RESOLUTION_WINDOW_DAYS} days; closed at the last close")
    return {"status": "PENDING", "entry_date": d0, "entry": entry, "rr": rr, "note": "trade open"}


def apply_one_at_a_time(rows, results):
    """rows: dicts with id, symbol, signal_date, sorted by (signal_date, id).
    results: {id: resolve_v2 result}. Marks a row DUPLICATE when an earlier
    counted trade on the same symbol had not exited by its signal date (an
    open or pending earlier trade blocks everything after it)."""
    busy_until = {}                      # symbol -> exit date, or date.max while open
    owner = {}                           # symbol -> id of the trade that is open
    for r in rows:
        res = results[r["id"]]
        sym = r["symbol"]
        if sym in busy_until and r["signal_date"] <= busy_until[sym]:
            results[r["id"]] = {"status": "DUPLICATE", "dup_of": owner[sym],
                                "note": "earlier signal on this symbol was still open"}
            continue
        if res["status"] in ("RESOLVED", "PENDING"):
            busy_until[sym] = res.get("outcome_date") or date.max
            owner[sym] = r["id"]
    return results


def _bars(symbol, start):
    # Yahoo intermittently answers a valid symbol with no data ("possibly
    # delisted" -- seen for GOOGL while testing this). Treating that as "no
    # price data" would wrongly mark real trades SKIPPED, so an empty answer
    # is a failed fetch: None, and recompute_all leaves that symbol's rows
    # exactly as they were until a later cycle succeeds.
    df = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        return None
    return [(i.date(), float(x.Open), float(x.High), float(x.Low), float(x.Close)) for i, x in df.iterrows()]


def recompute_all(today=None):
    """Recomputes the corrected result for every row from price data.
    Deterministic and idempotent: the same prices always give the same
    answer, so there is no stored state to drift. Legacy columns are never
    written. Returns the number of rows updated."""
    if not DATABASE_URL:
        return 0
    today = today or datetime.now(timezone.utc).date()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, symbol, signal_date, stop_price, target_price FROM signal_track "
                        "ORDER BY signal_date, id")
            rows = [dict(zip(("id", "symbol", "signal_date", "stop", "target"), x)) for x in cur.fetchall()]
    if not rows:
        return 0
    bars = {}
    for sym in {r["symbol"] for r in rows}:
        first = min(r["signal_date"] for r in rows if r["symbol"] == sym)
        try:
            bars[sym] = _bars(sym, first)
        except Exception as e:
            print(f"track_record: price history failed for {sym}: {type(e).__name__}: {e}")
            bars[sym] = None
    results = {}
    for r in rows:
        if bars[r["symbol"]] is None:      # fetch failed: leave this symbol's rows as they are
            results[r["id"]] = None
            continue
        results[r["id"]] = resolve_v2(r["symbol"], r["signal_date"], r["stop"], r["target"], bars[r["symbol"]], today)
    ok_rows = [r for r in rows if results[r["id"]] is not None]
    apply_one_at_a_time(ok_rows, results)

    n = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for r in ok_rows:
                x = results[r["id"]]
                cur.execute("""
                    UPDATE signal_track SET v2_status=%s, v2_entry_date=%s, v2_entry=%s, v2_rr=%s,
                        v2_outcome=%s, v2_outcome_date=%s, v2_outcome_price=%s, v2_return_pct=%s,
                        v2_note=%s, v2_dup_of=%s
                    WHERE id=%s
                """, (x["status"], x.get("entry_date"), x.get("entry"), x.get("rr"), x.get("outcome"),
                      x.get("outcome_date"), x.get("outcome_price"),
                      round(x["return_pct"], 4) if x.get("return_pct") is not None else None,
                      x.get("note"), x.get("dup_of"), r["id"]))
                n += 1
        conn.commit()
    return n


def resolve_pending():
    """Kept for alerts.py: one cycle now recomputes the corrected record."""
    return recompute_all()


# ── Statistics ────────────────────────────────────────────────────────────
def summarize(trades):
    """trades: RESOLVED rows (dicts with v2_outcome, v2_return_pct)."""
    n = len(trades)
    if not n:
        return {"trades": 0}
    rets = [t["v2_return_pct"] for t in trades]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    srt = sorted(rets)
    return {
        "trades":          n,
        "target_hits":     sum(1 for t in trades if t["v2_outcome"] == "WIN"),
        "stop_hits":       sum(1 for t in trades if t["v2_outcome"] == "LOSS"),
        "expired":         sum(1 for t in trades if t["v2_outcome"] == "EXPIRED"),
        "profitable_pct":  round(len(wins) / n * 100, 1),
        "avg_return_pct":  round(sum(rets) / n, 2),
        "median_return_pct": round(srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2, 2),
        "avg_win_pct":     round(gross_win / len(wins), 2) if wins else None,
        "avg_loss_pct":    round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor":   round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "worst_pct":       round(srt[0], 2),
        "best_pct":        round(srt[-1], 2),
    }


def get_track_record():
    empty = {"method": "corrected-2026-11-02", "total": 0, "recent": []}
    if not DATABASE_URL:
        return empty
    cols = ["id", "symbol", "signal_date", "rating", "score", "entry_price", "stop_price", "target_price",
            "outcome", "outcome_date", "return_pct", "signal_at", "price_at_signal", "rr_at_signal", "entry_ok",
            "v2_status", "v2_entry_date", "v2_entry", "v2_rr", "v2_outcome", "v2_outcome_date",
            "v2_outcome_price", "v2_return_pct", "v2_note", "v2_dup_of"]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(cols)} FROM signal_track ORDER BY signal_date DESC, id DESC")
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    resolved = [r for r in rows if r["v2_status"] == "RESOLVED"]
    by_rating = {k: summarize([r for r in resolved if r["rating"] == k]) for k in ("STRONG BUY", "BUY")}
    # Does the entry-quality gate help? Judged on the R:R at the actual
    # (next-open) entry, which exists for old and new rows alike.
    by_entry_rr = {
        "rr_ge_1_5": summarize([r for r in resolved if r["v2_rr"] is not None and r["v2_rr"] >= 1.5]),
        "rr_lt_1_5": summarize([r for r in resolved if r["v2_rr"] is not None and r["v2_rr"] < 1.5]),
    }
    counts = {s: sum(1 for r in rows if r["v2_status"] == s) for s in ("RESOLVED", "PENDING", "SKIPPED", "DUPLICATE")}
    counts["not_yet_computed"] = sum(1 for r in rows if r["v2_status"] is None)

    # The original (flawed) method, kept visible for transparency.
    legacy_res = [r for r in rows if r["outcome"] != "PENDING"]
    legacy = {}
    for k in ("STRONG BUY", "BUY"):
        lr = [r for r in legacy_res if r["rating"] == k]
        lw = [r for r in lr if r["outcome"] in ("WIN", "EXPIRED_WIN")]
        legacy[k] = {"resolved": len(lr), "win_rate": round(len(lw) / len(lr) * 100, 1) if lr else None,
                     "avg_return_pct": round(sum(r["return_pct"] for r in lr) / len(lr), 2) if lr else None}

    for r in rows:
        for k in ("signal_date", "outcome_date", "v2_entry_date", "v2_outcome_date"):
            r[k] = r[k].isoformat() if r[k] else None
        r["signal_at"] = r["signal_at"].isoformat() if r["signal_at"] else None
        for k in ("v2_entry", "v2_rr", "v2_outcome_price", "v2_return_pct"):
            if r[k] is not None:
                r[k] = round(float(r[k]), 4)

    return {
        "method":     "corrected-2026-11-02",
        "total":      len(rows),
        "counts":     counts,
        "overall":    summarize(resolved),
        "by_rating":  by_rating,
        "by_entry_rr": by_entry_rr,
        "min_trades_for_conclusion": MIN_TRADES_FOR_CONCLUSION,
        "window_days": RESOLUTION_WINDOW_DAYS,
        "legacy":     legacy,
        "recent":     rows[:100],
    }
