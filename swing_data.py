# ============================================================
# File: swing_data.py
# Date: 2026-11-02
# Author: Developer Team, getChartPulse
# Task: Deterministic number-crunching for the "Swing Trade Setup"
#       research report. Every figure the report shows as a
#       historical observation is computed HERE, in plain pandas,
#       never by the language model:
#         - dataset validation (rows, duplicates, ordering, NaNs,
#           OHLC consistency, zero volume, stale bars, missing
#           weekdays, possible unadjusted stock splits)
#         - SMA20 / SMA50 with their inputs, relative volume,
#           RSI(14), MACD(12,26,9), ATR(14), 20-day rate of change,
#           52-week range
#         - support / resistance zones from clustered swing pivots,
#           each with the dates it was touched
#       The model only receives the resulting facts and writes the
#       hypothetical scenarios around them.
#
#       Input: a DataFrame with Date, Open, High, Low, Close, Volume
#       -- either the site's Yahoo daily history or a user-uploaded
#       CSV (parse_csv). Nothing here does network I/O.
# ============================================================

import io
import math

import numpy as np
import pandas as pd

MIN_ROWS = 60          # SMA50 plus a little history
MAX_ROWS = 5000
MAX_CSV_BYTES = 1_000_000
PIVOT_K = 3            # a swing high/low must be the extreme of k bars each side
LOOKBACK_SR = 250      # ~1 trading year of pivots
SPLIT_RATIOS = (2, 3, 4, 5, 8, 10, 15, 20, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10)

_COLS = {"date": "Date", "open": "Open", "high": "High", "low": "Low",
         "close": "Close", "volume": "Volume"}


class DataError(ValueError):
    """Raised with a user-readable message when a dataset can't be used."""


def parse_csv(text):
    """Uploaded CSV -> (DataFrame, list of parse notes). Accepts any column
    order and case; needs Date, Open, High, Low, Close, Volume. 'Adj Close'
    is ignored (the Close column is what gets analysed, and the report says
    so). Raises DataError on anything it can't use."""
    if not text or not text.strip():
        raise DataError("The file is empty.")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise DataError("The file is larger than 1 MB -- upload daily bars only.")
    try:
        raw = pd.read_csv(io.StringIO(text))
    except Exception as e:
        raise DataError(f"Couldn't read it as CSV ({type(e).__name__}).")
    rename = {}
    for c in raw.columns:
        key = str(c).strip().lower().replace("_", " ")
        if key in _COLS:
            rename[c] = _COLS[key]
    df = raw.rename(columns=rename)
    missing = [c for c in _COLS.values() if c not in df.columns]
    if missing:
        raise DataError("Missing column(s): " + ", ".join(missing) +
                        ". Expected Date, Open, High, Low, Close, Volume.")
    notes = []
    if any(str(c).strip().lower() == "adj close" for c in raw.columns):
        notes.append("An 'Adj Close' column was present and ignored; the Close column was analysed.")
    df = df[list(_COLS.values())].copy()
    dates = pd.to_datetime(df["Date"], errors="coerce")
    bad_dates = int(dates.isna().sum())
    if bad_dates:
        notes.append(f"{bad_dates} row(s) with an unreadable date were dropped.")
    df["Date"] = dates
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
    df = df.dropna(subset=["Date"])
    if len(df) > MAX_ROWS:
        raise DataError(f"More than {MAX_ROWS} rows -- upload at most ~20 years of daily bars.")
    return df, notes


def _round(v, n=4):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), n)


def validate(df, notes=None):
    """Cleans and checks the dataset. Returns (clean_df, validation dict).
    Raises DataError if fewer than MIN_ROWS usable bars remain."""
    notes = list(notes or [])
    v = {"rows_received": int(len(df))}
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], utc=True, errors="coerce").dt.tz_localize(None).dt.normalize()

    v["was_sorted"] = bool(df["Date"].is_monotonic_increasing)
    if not v["was_sorted"]:
        notes.append("Rows were not in date order and were sorted oldest to newest.")
    df = df.sort_values("Date")

    dup = int(df["Date"].duplicated(keep="last").sum())
    v["duplicate_dates"] = dup
    if dup:
        notes.append(f"{dup} duplicate date(s) found; the last row for each date was kept.")
        df = df.drop_duplicates("Date", keep="last")

    nan_rows = int(df[["Open", "High", "Low", "Close"]].isna().any(axis=1).sum())
    v["rows_missing_prices"] = nan_rows
    if nan_rows:
        notes.append(f"{nan_rows} row(s) missing a price were dropped.")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])

    nonpos = int((df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1).sum())
    v["non_positive_prices"] = nonpos
    if nonpos:
        notes.append(f"{nonpos} row(s) with a zero or negative price were dropped.")
        df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]

    bad_ohlc = (df["High"] < df[["Open", "Close"]].max(axis=1) - 1e-9) | \
               (df["Low"] > df[["Open", "Close"]].min(axis=1) + 1e-9) | (df["High"] < df["Low"])
    v["inconsistent_ohlc_rows"] = int(bad_ohlc.sum())
    if v["inconsistent_ohlc_rows"]:
        notes.append(f"{v['inconsistent_ohlc_rows']} row(s) where High/Low don't bracket Open/Close "
                     "(kept, but treat those bars with caution).")

    df["Volume"] = df["Volume"].fillna(0)
    v["zero_volume_rows"] = int((df["Volume"] <= 0).sum())
    if v["zero_volume_rows"]:
        notes.append(f"{v['zero_volume_rows']} bar(s) with zero/missing volume -- these drag the "
                     "volume average down and inflate relative volume; check whether they are real.")

    # min_count=1 so the first row (all-NaN diff) isn't counted as "unchanged".
    same = (df[["Open", "High", "Low", "Close"]].diff().abs().sum(axis=1, min_count=1) == 0)
    stale = int((same & same.shift(1, fill_value=False)).sum())
    v["stale_repeated_bars"] = stale
    if stale:
        notes.append(f"{stale} bar(s) repeat the previous bar exactly for 3+ days -- possible stale data.")

    df = df.reset_index(drop=True)
    if len(df) < MIN_ROWS:
        raise DataError(f"Only {len(df)} usable daily bars -- at least {MIN_ROWS} are needed for a 50-day average.")

    # Missing observations: weekdays between consecutive bars. A single
    # missing weekday is usually an exchange holiday; 2+ in a row is a gap.
    d = df["Date"].values.astype("datetime64[D]")
    between = np.busday_count(d[:-1] + 1, d[1:])
    gaps = [(str(d[i])[:10], str(d[i + 1])[:10], int(n)) for i, n in enumerate(between) if n > 0]
    v["missing_weekdays_total"] = int(between.sum())
    v["single_day_gaps"] = sum(1 for g in gaps if g[2] == 1)
    multi = [g for g in gaps if g[2] >= 2]
    v["multi_day_gaps"] = [{"after": a, "before": b, "missing_weekdays": n} for a, b, n in multi[:10]]
    v["multi_day_gap_count"] = len(multi)
    if multi:
        notes.append(f"{len(multi)} gap(s) of 2+ consecutive missing weekdays -- likely missing data "
                     "(or a trading halt), not just holidays.")

    v["first_date"] = str(d[0])[:10]
    v["last_date"] = str(d[-1])[:10]
    v["rows_used"] = int(len(df))
    v["notes"] = notes
    return df, v


def detect_splits(df, known_splits=None):
    """Flags day-over-day moves that sit almost exactly on a common split
    ratio (e.g. close halves overnight) -- the signature of an unadjusted
    split. Real crashes can coincide with a ratio, so these are 'possible'."""
    out = []
    prev_close = df["Close"].shift(1)
    ratio = prev_close / df["Open"]
    for i in range(1, len(df)):
        r = ratio.iloc[i]
        if not np.isfinite(r) or 0.6 < r < 1.6:
            continue
        for k in SPLIT_RATIOS:
            if abs(r / k - 1) < 0.06:
                label = f"{int(round(k))}-for-1" if k >= 1 else f"1-for-{int(round(1 / k))} (reverse)"
                out.append({"date": str(df["Date"].iloc[i])[:10], "ratio": _round(r, 3), "looks_like": label,
                            "prev_close": _round(prev_close.iloc[i]), "open": _round(df["Open"].iloc[i])})
                break
    known = [{"date": str(pd.Timestamp(dt).date()), "ratio": _round(val, 4)} for dt, val in (known_splits or [])]
    return {"possible_unadjusted": out, "known_splits": known}


def _rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def _atr(df, n=14):
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def indicators(df):
    c, vol = df["Close"], df["Volume"]
    last = float(c.iloc[-1])
    sma20, sma50 = c.rolling(20).mean(), c.rolling(50).mean()
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    rsi, atr = _rsi(c), _atr(df)
    avg_vol20 = vol.iloc[-21:-1].mean()        # 20 sessions BEFORE the latest one
    yr = df.iloc[-252:]
    out = {
        "last_date": str(df["Date"].iloc[-1])[:10],
        "last_close": _round(last),
        "sma20": {"value": _round(sma20.iloc[-1]),
                  "calc": f"sum of last 20 closes {c.iloc[-20:].sum():,.4f} / 20",
                  "five_days_ago": _round(sma20.iloc[-6]),
                  "price_vs_pct": _round((last / sma20.iloc[-1] - 1) * 100, 2)},
        "sma50": {"value": _round(sma50.iloc[-1]),
                  "calc": f"sum of last 50 closes {c.iloc[-50:].sum():,.4f} / 50",
                  "five_days_ago": _round(sma50.iloc[-6]),
                  "price_vs_pct": _round((last / sma50.iloc[-1] - 1) * 100, 2)},
        "sma20_vs_sma50": "above" if sma20.iloc[-1] > sma50.iloc[-1] else "below",
        "relative_volume": {
            "value": _round(vol.iloc[-1] / avg_vol20, 2) if avg_vol20 else None,
            "calc": f"latest volume {vol.iloc[-1]:,.0f} / average of the prior 20 sessions {avg_vol20:,.0f}",
            "five_day_avg": _round(vol.iloc[-5:].mean() / avg_vol20, 2) if avg_vol20 else None,
        },
        "rsi14": {"value": _round(rsi.iloc[-1], 1), "calc": "Wilder RSI: 100 - 100/(1 + avg gain/avg loss), 14 sessions"},
        "macd": {"line": _round(macd.iloc[-1]), "signal": _round(signal.iloc[-1]),
                 "histogram": _round(macd.iloc[-1] - signal.iloc[-1]),
                 "histogram_5d_ago": _round(macd.iloc[-6] - signal.iloc[-6]),
                 "calc": "EMA12 - EMA26 of close; signal = EMA9 of that line"},
        "atr14": {"value": _round(atr.iloc[-1]), "pct_of_price": _round(atr.iloc[-1] / last * 100, 2),
                  "calc": "Wilder average of true range, 14 sessions"},
        "roc20_pct": {"value": _round((last / c.iloc[-21] - 1) * 100, 2),
                      "calc": f"{last:,.4f} / close 20 sessions ago {c.iloc[-21]:,.4f} - 1"},
        "range_52w": {"high": _round(yr["High"].max()), "high_date": str(yr.loc[yr["High"].idxmax(), "Date"])[:10],
                      "low": _round(yr["Low"].min()), "low_date": str(yr.loc[yr["Low"].idxmin(), "Date"])[:10],
                      "sessions": int(len(yr))},
        "recent_bars": [{"date": str(r.Date)[:10], "open": _round(r.Open), "high": _round(r.High),
                         "low": _round(r.Low), "close": _round(r.Close), "volume": int(r.Volume)}
                        for r in df.iloc[-10:].itertuples()],
    }
    return out, float(atr.iloc[-1])


def support_resistance(df, atr):
    """Clusters k-bar swing highs and lows from the last LOOKBACK_SR bars
    into zones (pivots within 0.6 x ATR of each other merge). A zone's
    evidence is its touch count and the dates of those touches."""
    d = df.iloc[-LOOKBACK_SR:].reset_index(drop=True)
    k = PIVOT_K
    piv = []
    for i in range(k, len(d) - k):
        win = d.iloc[i - k:i + k + 1]
        if d["High"].iloc[i] == win["High"].max():
            piv.append((float(d["High"].iloc[i]), str(d["Date"].iloc[i])[:10], "swing high"))
        if d["Low"].iloc[i] == win["Low"].min():
            piv.append((float(d["Low"].iloc[i]), str(d["Date"].iloc[i])[:10], "swing low"))
    piv.sort()
    zones, cur = [], []
    for p in piv:
        if cur and p[0] - cur[0][0] > 0.6 * atr:
            zones.append(cur)
            cur = []
        cur.append(p)
    if cur:
        zones.append(cur)

    last = float(df["Close"].iloc[-1])
    out = []
    for z in zones:
        lo, hi = min(p[0] for p in z), max(p[0] for p in z)
        if hi - lo < 0.2 * atr:                      # give a single-pivot zone some width
            mid = (hi + lo) / 2
            lo, hi = mid - 0.15 * atr, mid + 0.15 * atr
        dates = sorted(p[1] for p in z)
        out.append({"low": _round(lo), "high": _round(hi), "touches": len(z),
                    "first_touch": dates[0], "last_touch": dates[-1], "touch_dates": dates[-6:],
                    "kinds": sorted({p[2] for p in z}),
                    "type": "resistance" if lo > last else "support" if hi < last else "price is inside this zone",
                    "distance_pct": _round(((lo if lo > last else hi) / last - 1) * 100, 2)})
    # Keep the zones that matter for a swing: up to 3 nearest each side,
    # dropping one-touch zones unless nothing better exists that side.
    def pick(kind, key):
        cands = [z for z in out if z["type"] == kind]
        strong = [z for z in cands if z["touches"] >= 2] or cands
        return sorted(strong, key=key)[:3]
    sup = pick("support", lambda z: -z["high"])
    res = pick("resistance", lambda z: z["low"])
    inside = [z for z in out if z["type"] == "price is inside this zone"]
    return {"method": f"{PIVOT_K}-bar swing highs/lows over the last {min(LOOKBACK_SR, len(df))} sessions, "
                      "merged when within 0.6 x ATR(14); touches = pivots in the zone",
            "support": sup, "resistance": res, "current_zone": inside[:1]}


def analyze(df, notes=None, known_splits=None, source="", days=10):
    """Full fact sheet handed to the model and shown on the page."""
    clean, val = validate(df, notes)
    ind, atr = indicators(clean)
    return {
        "source": source,
        "trade_days": int(days),
        "validation": val,
        "splits": detect_splits(clean, known_splits),
        "indicators": ind,
        "levels": support_resistance(clean, atr),
        # Last bar date + N trading days (weekdays), for the event-window check.
        "window_end": str(np.busday_offset(np.datetime64(val["last_date"]), int(days), roll="forward")),
    }
