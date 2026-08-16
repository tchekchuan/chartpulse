from flask import Flask, render_template, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# In-memory is fine here — Render runs this app as a single gunicorn worker,
# so there's only one process to keep the counters in.
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY
# ══════════════════════════════════════════════════════════════════════════════

def calc_atr(df, period=14):
    """
    Average True Range — the volatility ruler used to make every other
    algorithm adaptive to the specific stock being analysed.
    """
    h = df["High"]
    l = df["Low"]
    pc = df["Close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else float((h - l).mean())


def _dt_col(df):
    return "Datetime" if "Datetime" in df.columns else "Date"


# ══════════════════════════════════════════════════════════════════════════════
#  1. PIVOT POINTS  (Classic + Fibonacci)
#  FIX: intraday intervals now correctly use the PREVIOUS TRADING DAY's H/L/C,
#       not just the previous candle.
# ══════════════════════════════════════════════════════════════════════════════

def pivot_points(df, interval="1d"):
    """
    Returns both Classic and Fibonacci pivot points.

    Intraday (≤1h): anchor on prior full trading day's H/L/C.
    Daily/Weekly:   anchor on prior completed candle's H/L/C.
    """
    intraday = interval in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h")

    if intraday:
        dc = _dt_col(df)
        df2 = df.copy()
        df2["_date"] = pd.to_datetime(df2[dc]).dt.normalize()
        daily = (
            df2.groupby("_date")
            .agg(High=("High", "max"), Low=("Low", "min"), Close=("Close", "last"))
        )
        if len(daily) >= 2:
            prev = daily.iloc[-2]
        else:
            prev = df.iloc[-2]
        H, L, C = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    else:
        row = df.iloc[-2]
        H, L, C = float(row["High"]), float(row["Low"]), float(row["Close"])

    P   = (H + L + C) / 3
    rng = H - L

    def r(v):
        return round(v, 4)

    classic = {
        "PP": r(P),
        "R1": r(2 * P - L),
        "R2": r(P + rng),
        "R3": r(H + 2 * (P - L)),
        "S1": r(2 * P - H),
        "S2": r(P - rng),
        "S3": r(L - 2 * (H - P)),
    }

    fibonacci = {
        "PP": r(P),
        "R1": r(P + 0.382 * rng),
        "R2": r(P + 0.618 * rng),
        "R3": r(P + 1.000 * rng),
        "S1": r(P - 0.382 * rng),
        "S2": r(P - 0.618 * rng),
        "S3": r(P - 1.000 * rng),
    }

    return {"classic": classic, "fibonacci": fibonacci}


# ══════════════════════════════════════════════════════════════════════════════
#  2. FRACTAL SUPPORT & RESISTANCE  with Strength Scoring
#  FIX: ATR-based clustering tolerance, touch/bounce counting, recency weighting
# ══════════════════════════════════════════════════════════════════════════════

def fractal_sr(df, window=5):
    """
    Detects swing highs (resistance) and swing lows (support) using
    a fractal (n-bar high/low) approach, then scores each level by:

    • Touch count   — how many candles approached AND bounced from the level
    • Recency       — more recent levels carry higher weight
    • Cluster size  — multiple nearby swings = stronger confluence zone

    Returns structured dicts with price, strength (1–5), touches, recency.
    """
    highs   = df["High"].values
    lows    = df["Low"].values
    closes  = df["Close"].values
    volumes = df["Volume"].values
    n       = len(df)

    atr_val   = calc_atr(df)
    # Cluster tolerance: 1.0× ATR — price levels within this distance are the same zone
    cluster_tol = atr_val * 1.0
    # Touch tolerance: 0.6× ATR — price came "close enough" to test the level
    touch_tol   = atr_val * 0.6

    # ── Find fractal swing points ─────────────────────────────────────────────
    swing_highs = []   # (price, bar_index, volume)
    swing_lows  = []

    for i in range(window, n - window):
        lo_win = slice(i - window, i + window + 1)
        if highs[i] >= np.max(highs[lo_win]):
            swing_highs.append((float(highs[i]), i, float(volumes[i])))
        if lows[i]  <= np.min(lows[lo_win]):
            swing_lows.append((float(lows[i]),  i, float(volumes[i])))

    # ── Count confirmed touches at a given price level ────────────────────────
    def count_touches(price, is_resistance):
        """
        A touch = candle reached within touch_tol of the level AND
        closed on the "correct" side (price respected / bounced from level).
        """
        touches = 0
        for j in range(n):
            if is_resistance:
                # High reached up to level but close stayed below → bounce down
                if (highs[j] >= price - touch_tol and
                        highs[j] <= price + touch_tol and
                        closes[j] < price + touch_tol):
                    touches += 1
            else:
                # Low dropped to level but close stayed above → bounce up
                if (lows[j] <= price + touch_tol and
                        lows[j] >= price - touch_tol and
                        closes[j] > price - touch_tol):
                    touches += 1
        return max(0, touches - 1)   # subtract the origin candle

    # ── Cluster nearby swings, score, and sort ────────────────────────────────
    def cluster_and_score(swings, is_resistance):
        if not swings:
            return []

        swings_sorted = sorted(swings, key=lambda x: x[0])
        clusters = [[swings_sorted[0]]]
        for s in swings_sorted[1:]:
            if abs(s[0] - clusters[-1][-1][0]) <= cluster_tol:
                clusters[-1].append(s)
            else:
                clusters.append([s])

        results = []
        for cluster in clusters:
            prices  = np.array([c[0] for c in cluster])
            vols    = np.array([c[2] for c in cluster]) + 1      # avoid zero-weight
            bar_idx = max(c[1] for c in cluster)                 # most recent bar

            # Volume-weighted representative price for the zone
            price = float(np.average(prices, weights=vols))

            touches  = count_touches(price, is_resistance)
            recency  = bar_idx / n                               # 0–1, higher = newer
            cluster_sz = len(cluster)

            # Strength 1–5:
            #   base from touches + cluster confluence + recency bonus
            raw_str  = 1 + min(2, touches) + min(1, cluster_sz - 1) + round(recency * 1.5)
            strength = int(min(5, max(1, raw_str)))

            results.append({
                "price":    round(price, 4),
                "touches":  touches + cluster_sz,
                "strength": strength,
                "recency":  round(recency, 2),
                "fresh":    touches == 0,       # never tested since formed
            })

        results.sort(key=lambda x: (x["strength"], x["recency"]), reverse=True)
        return results

    resistances = cluster_and_score(swing_highs, is_resistance=True)
    supports    = cluster_and_score(swing_lows,  is_resistance=False)

    current = float(closes[-1])
    # ATR-adaptive filter: show levels within 20× ATR of current price
    max_dist = atr_val * 20

    resistances = [r for r in resistances
                   if r["price"] >= current * 0.985 and
                   abs(r["price"] - current) <= max_dist][:6]
    supports    = [s for s in supports
                   if s["price"] <= current * 1.015 and
                   abs(s["price"] - current) <= max_dist][:6]

    return {"resistance": resistances, "support": supports}


# ══════════════════════════════════════════════════════════════════════════════
#  3. VOLUME PROFILE  (POC · VAH · VAL · HVN · LVN)
#  FIX: 200 bins, proper POC detection, Value Area 70% expansion, LVN zones
# ══════════════════════════════════════════════════════════════════════════════

def volume_profile(df, bins=200):
    """
    Distributes each candle's volume uniformly across its High–Low range
    (standard OHLCV approximation used by institutional-grade platforms).

    Returns:
      poc  — Point of Control (highest volume price)
      vah  — Value Area High  (upper bound of zone containing 70% of volume)
      val  — Value Area Low   (lower bound)
      hvn  — Top High-Volume Nodes (strong S/R magnets)
      lvn  — Low-Volume Nodes (price moves quickly through these)
    """
    lo_min = df["Low"].min()
    hi_max = df["High"].max()
    edges  = np.linspace(lo_min, hi_max, bins + 1)
    vols   = np.zeros(bins)

    for _, row in df.iterrows():
        lo, hi, vol = float(row["Low"]), float(row["High"]), float(row["Volume"])
        rng = hi - lo
        if rng == 0:
            idx = int(np.searchsorted(edges, float(row["Close"])) - 1)
            vols[max(0, min(bins - 1, idx))] += vol
        else:
            # vectorised overlap calculation
            bin_lo = edges[:-1]
            bin_hi = edges[1:]
            overlap = np.maximum(0, np.minimum(hi, bin_hi) - np.maximum(lo, bin_lo))
            vols += vol * overlap / rng

    # POC
    poc_idx = int(np.argmax(vols))
    poc     = round(float((edges[poc_idx] + edges[poc_idx + 1]) / 2), 4)

    # Value Area — start from POC, expand whichever side adds more volume
    total_vol  = vols.sum()
    target     = total_vol * 0.70
    lo_idx = hi_idx = poc_idx
    accumulated = vols[poc_idx]

    while accumulated < target:
        can_up   = hi_idx + 1 < bins
        can_down = lo_idx - 1 >= 0
        if not can_up and not can_down:
            break
        up_vol   = vols[hi_idx + 1] if can_up   else -1
        down_vol = vols[lo_idx - 1] if can_down else -1
        if up_vol >= down_vol:
            hi_idx     += 1
            accumulated += vols[hi_idx]
        else:
            lo_idx     -= 1
            accumulated += vols[lo_idx]

    vah = round(float((edges[hi_idx] + edges[hi_idx + 1]) / 2), 4)
    val = round(float((edges[lo_idx] + edges[lo_idx + 1]) / 2), 4)

    # HVN — top volume nodes OUTSIDE the POC immediate area
    poc_exclusion = (hi_max - lo_min) * 0.025
    sorted_idx    = np.argsort(vols)[::-1]
    hvn = []
    for idx in sorted_idx:
        p = float((edges[idx] + edges[idx + 1]) / 2)
        if abs(p - poc) > poc_exclusion and all(abs(p - h) > poc_exclusion for h in hvn):
            hvn.append(round(p, 4))
        if len(hvn) >= 5:
            break

    # LVN — lowest volume nodes (price tends to move quickly through these)
    sorted_asc = np.argsort(vols)
    lvn = []
    for idx in sorted_asc:
        p = float((edges[idx] + edges[idx + 1]) / 2)
        # Only meaningful LVNs — not at the extremes of the range
        pct = (p - lo_min) / (hi_max - lo_min) if (hi_max - lo_min) > 0 else 0
        if 0.05 < pct < 0.95 and all(abs(p - l) > poc_exclusion for l in lvn):
            lvn.append(round(p, 4))
        if len(lvn) >= 3:
            break

    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "hvn": sorted(hvn),
        "lvn": sorted(lvn),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  4. MOVING AVERAGES  (dynamic S/R)
# ══════════════════════════════════════════════════════════════════════════════

def moving_averages(df):
    """
    Key moving averages.  Price consistently bounces off 20/50/200 SMA
    and the 200 EMA in trending markets — they act as dynamic S/R.
    Only returned when there is enough data to compute them reliably.
    """
    c  = df["Close"]
    r  = {}
    for period, name in [(20, "SMA20"), (50, "SMA50"), (200, "SMA200")]:
        if len(c) >= period:
            v = c.rolling(period).mean().iloc[-1]
            if not np.isnan(v):
                r[name] = round(float(v), 4)
    for period, name in [(9, "EMA9"), (21, "EMA21")]:
        if len(c) >= period:
            v = c.ewm(span=period, adjust=False).mean().iloc[-1]
            if not np.isnan(v):
                r[name] = round(float(v), 4)
    return r


# ══════════════════════════════════════════════════════════════════════════════
#  5. PSYCHOLOGICAL ROUND-NUMBER LEVELS
# ══════════════════════════════════════════════════════════════════════════════

def round_number_levels(price):
    """
    Retail and institutional traders anchor orders on round numbers.
    Step size scales with price magnitude so levels are always meaningful.
    """
    if   price < 5:     step = 0.5
    elif price < 20:    step = 1
    elif price < 50:    step = 2.5
    elif price < 100:   step = 5
    elif price < 500:   step = 10
    elif price < 1000:  step = 25
    elif price < 5000:  step = 50
    else:               step = 100

    base   = round(price / step) * step
    levels = [round(base + step * i, 4) for i in range(-5, 6)]
    # Keep only levels within 12% of current price
    return sorted(l for l in levels if l > 0 and abs(l - price) / price <= 0.12)


# ══════════════════════════════════════════════════════════════════════════════
#  6. PREVIOUS PERIOD HIGHS / LOWS
# ══════════════════════════════════════════════════════════════════════════════

def period_levels(df):
    """
    Prior week and prior month high/low are among the most-watched S/R
    levels by professional traders — frequently used as take-profit,
    stop-loss, and breakout targets.
    """
    dc  = _dt_col(df)
    df2 = df.copy()
    df2["_dt"] = pd.to_datetime(df2[dc])
    df2 = df2.set_index("_dt").sort_index()

    result = {}

    # Previous week high/low
    try:
        weekly = df2.resample("W").agg(High=("High", "max"), Low=("Low", "min"))
        weekly = weekly.dropna()
        if len(weekly) >= 2:
            pw = weekly.iloc[-2]
            result["prev_week_high"] = round(float(pw["High"]), 4)
            result["prev_week_low"]  = round(float(pw["Low"]),  4)
    except Exception:
        pass

    # Previous month high/low
    try:
        for freq in ("ME", "M"):          # ME = pandas ≥2.2, M = older
            try:
                monthly = df2.resample(freq).agg(High=("High", "max"), Low=("Low", "min"))
                monthly = monthly.dropna()
                if len(monthly) >= 2:
                    pm = monthly.iloc[-2]
                    result["prev_month_high"] = round(float(pm["High"]), 4)
                    result["prev_month_low"]  = round(float(pm["Low"]),  4)
                break
            except Exception:
                continue
    except Exception:
        pass

    # Full-period high/low (52-week if data spans that far)
    result["period_high"] = round(float(df["High"].max()), 4)
    result["period_low"]  = round(float(df["Low"].min()),  4)

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  7. MOMENTUM INDICATORS  (RSI-14 · MACD 12/26/9)
#  Used by: Adam Khoo (RSI zones, MACD cross), Colin Seow (momentum confirmation)
# ══════════════════════════════════════════════════════════════════════════════

def momentum_indicators(df):
    """
    RSI-14 with Wilder's smoothing and MACD(12,26,9).
    Returns current values, zones, cross signals, and full RSI time-series
    for the chart mini-pane.
    """
    c = df["Close"]

    # RSI-14 (Wilder's EMA)
    delta    = c.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs       = avg_gain / (avg_loss + 1e-10)
    rsi      = 100 - (100 / (1 + rs))

    rsi_now  = round(float(rsi.iloc[-1]), 2)
    rsi_prev = round(float(rsi.iloc[-2]), 2) if len(rsi) >= 2 else rsi_now

    # MACD(12, 26, 9)
    ema12   = c.ewm(span=12, adjust=False).mean()
    ema26   = c.ewm(span=26, adjust=False).mean()
    macd    = ema12 - ema26
    sig     = macd.ewm(span=9, adjust=False).mean()
    hist    = macd - sig

    macd_now   = float(macd.iloc[-1])
    sig_now    = float(sig.iloc[-1])
    hist_now   = float(hist.iloc[-1])
    hist_prev  = float(hist.iloc[-2]) if len(hist) >= 2 else hist_now
    macd_prev  = float(macd.iloc[-2]) if len(macd) >= 2 else macd_now
    sig_prev   = float(sig.iloc[-2])  if len(sig)  >= 2 else sig_now

    # RSI zone labels
    if rsi_now <= 30:    rsi_zone = "oversold"
    elif rsi_now >= 70:  rsi_zone = "overbought"
    elif rsi_now >= 50:  rsi_zone = "bullish"
    else:                rsi_zone = "bearish"

    # RSI time-series for chart pane
    dc = _dt_col(df)
    rsi_ts = []
    for i, (_, row) in enumerate(df.iterrows()):
        v = rsi.iloc[i]
        if not np.isnan(v):
            ts = row["Datetime"] if "Datetime" in row else row["Date"]
            rsi_ts.append({"time": int(pd.Timestamp(ts).timestamp()),
                           "value": round(float(v), 2)})

    # Volume vs 20-day average -- classic TA treats a breakout/crossover on
    # above-average volume as more reliable, and one on thin volume as
    # weaker/more likely to fail. Used to adjust MACD crossover confidence.
    vol        = df["Volume"] if "Volume" in df.columns else pd.Series([0])
    avg_vol_20 = vol.rolling(20).mean().iloc[-1]
    vol_now    = float(vol.iloc[-1])
    vol_ratio  = round(vol_now / avg_vol_20, 2) if avg_vol_20 and not np.isnan(avg_vol_20) and avg_vol_20 > 0 else 1.0

    return {
        "rsi":          rsi_now,
        "rsi_prev":     rsi_prev,
        "rsi_zone":     rsi_zone,
        "macd":         round(macd_now, 4),
        "macd_signal":  round(sig_now, 4),
        "macd_hist":    round(hist_now, 4),
        "macd_above":   macd_now > sig_now,
        "macd_cross_up": macd_now > sig_now and macd_prev <= sig_prev,
        "macd_cross_dn": macd_now < sig_now and macd_prev >= sig_prev,
        "hist_rising":  hist_now > hist_prev,
        "rsi_series":   rsi_ts,
        "volume_ratio": vol_ratio,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  8. TREND STAGE  (Wyckoff / Stan Weinstein — used by Adam Khoo)
#  Stage 2 = ideal buy zone · Stage 4 = ideal sell/short zone
# ══════════════════════════════════════════════════════════════════════════════

def trend_stage(df, mas):
    """
    Classifies the stock into one of four Weinstein stages:
      1 — Accumulation / Base (sideways after downtrend)
      2 — Uptrend / Markup   ← Adam Khoo's ideal BUY zone
      3 — Distribution / Top (sideways after uptrend)
      4 — Downtrend / Markdown ← ideal SELL / avoid zone
    """
    c       = df["Close"]
    current = float(c.iloc[-1])

    sma20  = mas.get("SMA20")
    sma50  = mas.get("SMA50")
    sma200 = mas.get("SMA200")

    if sma200 is None:
        return {"stage": 0, "label": "N/A — need more data", "color": "#8b949e", "bias": "neutral"}

    above_200 = current > sma200
    ma_bull   = sma50 is not None and sma50 > sma200
    ma_bear   = sma50 is not None and sma50 < sma200

    # Is SMA50 trending up over last 10 bars?
    sma50_rising = False
    if sma50 and len(c) >= 60:
        sma50_series = c.rolling(50).mean()
        v10 = sma50_series.iloc[-10]
        if not np.isnan(v10):
            sma50_rising = sma50 > float(v10)

    if above_200 and ma_bull:
        stage, label, bias = 2, "Stage 2 — Uptrend ↑", "bullish"
    elif above_200 and not ma_bull:
        if sma50_rising:
            stage, label, bias = 1, "Stage 1→2 Transition", "bullish"
        else:
            stage, label, bias = 3, "Stage 3 — Distribution", "neutral"
    elif not above_200 and ma_bear:
        stage, label, bias = 4, "Stage 4 — Downtrend ↓", "bearish"
    else:
        stage, label, bias = 1, "Stage 1 — Base / Accumulation", "neutral"

    color_map = {1: "#e3b341", 2: "#3fb950", 3: "#f8a723", 4: "#f85149"}
    return {
        "stage":        stage,
        "label":        label,
        "bias":         bias,
        "color":        color_map.get(stage, "#8b949e"),
        "above_sma200": above_200,
        "ma_aligned":   ma_bull,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  9. CANDLESTICK PATTERNS  (Colin Seow / Adam Khoo entry timing)
# ══════════════════════════════════════════════════════════════════════════════

def candlestick_patterns(df, atr_val):
    """
    Detects high-probability reversal and continuation patterns on the
    last 3 candles.  Used for precise entry timing after a signal zone
    is identified (Colin Seow / Adam Khoo approach).
    """
    if len(df) < 3:
        return []

    def parse(row):
        o, h, l, c_val = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        body  = abs(c_val - o)
        upper = h - max(o, c_val)
        lower = min(o, c_val) - l
        return {"o": o, "h": h, "l": l, "c": c_val,
                "body": body, "uw": upper, "lw": lower, "bull": c_val >= o}

    c0 = parse(df.iloc[-1])   # latest
    c1 = parse(df.iloc[-2])
    c2 = parse(df.iloc[-3])
    min_body = atr_val * 0.04

    patterns = []

    # ── Bullish Patterns ──────────────────────────────────────────────────────
    # Bullish Engulfing
    if (c0["bull"] and not c1["bull"] and
            c0["o"] <= c1["c"] and c0["c"] >= c1["o"] and c0["body"] > c1["body"]):
        patterns.append({"name": "Bullish Engulfing", "type": "bullish", "strength": 4})

    # Hammer (long lower wick, small body at top, after down candle)
    if (c0["lw"] >= c0["body"] * 2.0 and c0["uw"] <= c0["body"] * 0.5 and
            c0["body"] >= min_body and not c1["bull"]):
        patterns.append({"name": "Hammer", "type": "bullish", "strength": 3})

    # Bullish Pin Bar (dominant lower wick — Colin Seow price action)
    if (c0["lw"] >= atr_val * 0.5 and
            c0["lw"] >= (c0["body"] + c0["uw"]) * 1.5):
        patterns.append({"name": "Bullish Pin Bar", "type": "bullish", "strength": 4})

    # Morning Star (3-candle bullish reversal)
    if (not c2["bull"] and c2["body"] > atr_val * 0.3 and
            c1["body"] < c2["body"] * 0.4 and
            c0["bull"] and c0["c"] > (c2["o"] + c2["c"]) / 2):
        patterns.append({"name": "Morning Star", "type": "bullish", "strength": 5})

    # ── Bearish Patterns ──────────────────────────────────────────────────────
    # Bearish Engulfing
    if (not c0["bull"] and c1["bull"] and
            c0["o"] >= c1["c"] and c0["c"] <= c1["o"] and c0["body"] > c1["body"]):
        patterns.append({"name": "Bearish Engulfing", "type": "bearish", "strength": 4})

    # Shooting Star (long upper wick after up candle)
    if (c0["uw"] >= c0["body"] * 2.0 and c0["lw"] <= c0["body"] * 0.5 and
            c0["body"] >= min_body and c1["bull"]):
        patterns.append({"name": "Shooting Star", "type": "bearish", "strength": 3})

    # Bearish Pin Bar (dominant upper wick)
    if (c0["uw"] >= atr_val * 0.5 and
            c0["uw"] >= (c0["body"] + c0["lw"]) * 1.5):
        patterns.append({"name": "Bearish Pin Bar", "type": "bearish", "strength": 4})

    # Evening Star (3-candle bearish reversal)
    if (c2["bull"] and c2["body"] > atr_val * 0.3 and
            c1["body"] < c2["body"] * 0.4 and
            not c0["bull"] and c0["c"] < (c2["o"] + c2["c"]) / 2):
        patterns.append({"name": "Evening Star", "type": "bearish", "strength": 5})

    # ── Neutral ───────────────────────────────────────────────────────────────
    if (c0["body"] < atr_val * 0.04 and
            (c0["uw"] + c0["lw"]) > atr_val * 0.25):
        patterns.append({"name": "Doji — Indecision", "type": "neutral", "strength": 2})

    return patterns


# ══════════════════════════════════════════════════════════════════════════════
#  10. FUNDAMENTALS  (Warren Buffett value-investing metrics)
# ══════════════════════════════════════════════════════════════════════════════

_FUNDAMENTALS_CACHE = {}     # symbol -> (expires_at, data)
_FUNDAMENTALS_TTL_OK   = 6 * 3600   # successful fetch: fundamentals barely move intraday
_FUNDAMENTALS_TTL_FAIL = 10 * 60    # failed fetch (e.g. rate limited): retry sooner, but not immediately


FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")


def _fetch_finnhub_fundamentals(sym):
    """
    Fetches fundamentals from Finnhub instead of yfinance's ticker.info —
    Yahoo rate-limits/blocks that specific endpoint from cloud hosting IPs
    (confirmed via production logs: YFRateLimitError on every call), while
    Finnhub's free tier (60 req/min) has no such issue for this app's scale.
    """
    profile = requests.get(
        "https://finnhub.io/api/v1/stock/profile2",
        params={"symbol": sym, "token": FINNHUB_API_KEY}, timeout=8,
    ).json()
    metric = requests.get(
        "https://finnhub.io/api/v1/stock/metric",
        params={"symbol": sym, "metric": "all", "token": FINNHUB_API_KEY}, timeout=8,
    ).json().get("metric", {})

    def safe(d, key, scale=1, dec=2):
        v = d.get(key)
        if v is None:
            return None
        try:
            return round(float(v) * scale, dec)
        except (TypeError, ValueError):
            return None

    return {
        "pe":       safe(metric, "peTTM"),
        "forward_pe": safe(metric, "forwardPE"),
        "pb":       safe(metric, "pbQuarterly"),
        "roe":      safe(metric, "roeTTM"),                        # already %
        "eps":      safe(metric, "epsTTM"),
        "eps_growth": safe(metric, "epsGrowthTTMYoy"),              # already %
        "rev_growth": safe(metric, "revenueGrowthTTMYoy"),          # already %
        "profit_margin": safe(metric, "netProfitMarginTTM"),        # already %
        "debt_equity": safe(metric, "totalDebt/totalEquityQuarterly", scale=100),
        "div_yield": safe(metric, "currentDividendYieldTTM"),       # already %
        "beta":     safe(metric, "beta"),
        "market_cap": safe(profile, "marketCapitalization", scale=1_000_000, dec=0),
        "name":     profile.get("name"),
        "sector":   profile.get("finnhubIndustry"),
        "industry": None,
    }


def get_fundamentals(ticker_obj):
    """
    Extracts key Buffett-style fundamental metrics.
    Scores the stock 0–5 on value quality (ROE, P/E, debt, EPS growth).

    Cached per symbol — fundamentals barely move intraday, and yfinance's
    ticker.info call (used as a fallback below) hits a far more
    rate-limited Yahoo endpoint than price/candle history.
    """
    sym = getattr(ticker_obj, "ticker", None)
    now = time.time()
    if sym and sym in _FUNDAMENTALS_CACHE:
        expires_at, cached = _FUNDAMENTALS_CACHE[sym]
        if now < expires_at:
            return cached

    info = None
    if FINNHUB_API_KEY and sym:
        try:
            fh = _fetch_finnhub_fundamentals(sym)
            if fh.get("pe") or fh.get("market_cap") or fh.get("name"):
                info = fh
        except Exception as e:
            app.logger.warning(f"get_fundamentals: Finnhub failed for {sym}: {type(e).__name__}: {e}")

    if info is None:
        try:
            yf_info = ticker_obj.info
        except Exception as e:
            app.logger.warning(f"get_fundamentals: ticker.info failed for {sym}: {type(e).__name__}: {e}")
            if sym:
                _FUNDAMENTALS_CACHE[sym] = (now + _FUNDAMENTALS_TTL_FAIL, {})
            return {}

        def safe(key, scale=1, dec=2):
            v = yf_info.get(key)
            if v is None or v == "N/A":
                return None
            try:
                return round(float(v) * scale, dec)
            except (TypeError, ValueError):
                return None

        info = {
            "pe":       safe("trailingPE"),
            "forward_pe": safe("forwardPE"),
            "pb":       safe("priceToBook"),
            "roe":      safe("returnOnEquity",        scale=100),
            "eps":      safe("trailingEps"),
            "eps_growth": safe("earningsQuarterlyGrowth", scale=100),
            "rev_growth": safe("revenueGrowth",           scale=100),
            "profit_margin": safe("profitMargins",        scale=100),
            "debt_equity": safe("debtToEquity"),
            "div_yield": safe("dividendYield",            scale=100),
            "beta":     safe("beta"),
            "market_cap": safe("marketCap", dec=0),
            "name":     yf_info.get("longName") or yf_info.get("shortName"),
            "sector":   yf_info.get("sector"),
            "industry": yf_info.get("industry"),
        }

    pe, fwd_pe, pb, roe, eps, eps_g, rev_g, margin, de, divy, beta, mktcap, name, sector, industry = (
        info["pe"], info["forward_pe"], info["pb"], info["roe"], info["eps"],
        info["eps_growth"], info["rev_growth"], info["profit_margin"], info["debt_equity"],
        info["div_yield"], info["beta"], info["market_cap"], info["name"], info["sector"], info["industry"],
    )

    # Buffett value score (0–5)
    score, reasons = 0, []
    if pe and 0 < pe < 25:
        score += 1; reasons.append(f"P/E {pe:.1f}")
    if roe and roe > 15:
        score += 2; reasons.append(f"ROE {roe:.0f}%")
    if de is not None and de < 50:
        score += 1; reasons.append("Low debt")
    if eps_g and eps_g > 10:
        score += 1; reasons.append(f"EPS +{eps_g:.0f}%")
    if margin and margin > 10:
        score += 1; reasons.append(f"Margin {margin:.0f}%")

    result = {
        "pe":            pe,
        "forward_pe":    fwd_pe,
        "pb":            pb,
        "roe":           roe,
        "eps":           eps,
        "eps_growth":    eps_g,
        "rev_growth":    rev_g,
        "profit_margin": margin,
        "debt_equity":   de,
        "div_yield":     divy,
        "beta":          beta,
        "market_cap":    mktcap,
        "name":          name,
        "sector":        sector,
        "industry":      industry,
        "value_score":   min(score, 5),
        "value_reasons": reasons[:3],
    }
    if sym:
        _FUNDAMENTALS_CACHE[sym] = (now + _FUNDAMENTALS_TTL_OK, result)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  11. TRADE SIGNALS  (Confluence + Stage + RSI + MACD + R:R)
#  Framework: Adam Khoo (Stage/RSI/MACD), Colin Seow (S/R confluence),
#             Jesse Livermore (trend direction), Peter Lynch (quality filter)
# ══════════════════════════════════════════════════════════════════════════════

def trade_signals(df, pivot_data, fractals, vp, mas, rounds, periods, momentum, stage):
    """
    Finds high-probability Buy and Sell zones by:
    1. Collecting all S/R candidates from every analysis
    2. Clustering nearby levels (price confluence)
    3. Scoring each zone with Trend Stage + RSI + MACD + R:R
    4. Assigning a 1–5 ★ quality rating
    Returns top-3 buy zones and top-3 sell zones.
    """
    atr_val     = calc_atr(df)
    current     = float(df["Close"].iloc[-1])
    cluster_tol = min(atr_val * 1.2, current * 0.015)
    pp          = pivot_data["classic"]

    def _collect(is_buy):
        out = []

        def add(price, label, weight):
            if is_buy  and price < current * 1.005:
                out.append((float(price), label, weight))
            if not is_buy and price > current * 0.995:
                out.append((float(price), label, weight))

        src = fractals["support"] if is_buy else fractals["resistance"]
        tag = "FS" if is_buy else "FR"
        for lvl in src:
            add(lvl["price"], f"{tag}{lvl['strength']}", lvl["strength"])

        keys = [("S1",2),("S2",2),("S3",1)] if is_buy else [("R1",2),("R2",2),("R3",1)]
        for key, wt in keys:
            add(pp[key], f"Pvt {key}", wt)

        if is_buy:
            add(vp["val"], "VAL", 3)
            add(vp["poc"], "POC", 4)
            for i, v in enumerate(vp["hvn"]): add(v, f"HVN{i+1}", 2)
        else:
            add(vp["vah"], "VAH", 3)
            add(vp["poc"], "POC", 4)
            for i, v in enumerate(vp["hvn"]): add(v, f"HVN{i+1}", 2)

        ma_wt = {"200": 4, "50": 3, "20": 2, "21": 2, "9": 1}
        for name, val in mas.items():
            for k, wt in ma_wt.items():
                if k in name:
                    add(val, name, wt)
                    break

        for v in rounds:
            lbl = f"${v:.0f}" if v >= 10 else f"${v:.1f}"
            add(v, lbl, 1)

        if is_buy:
            for key, lbl, wt in [("prev_week_low","Prev Wk L",2),("prev_month_low","Prev Mo L",3)]:
                if key in periods: add(periods[key], lbl, wt)
        else:
            for key, lbl, wt in [("prev_week_high","Prev Wk H",2),("prev_month_high","Prev Mo H",3)]:
                if key in periods: add(periods[key], lbl, wt)

        return out

    def cluster_and_rank(cands):
        if not cands:
            return []
        cands_s  = sorted(cands, key=lambda x: x[0])
        clusters = [[cands_s[0]]]
        for c in cands_s[1:]:
            if abs(c[0] - clusters[-1][-1][0]) <= cluster_tol:
                clusters[-1].append(c)
            else:
                clusters.append([c])

        results = []
        for cl in clusters:
            prices  = [c[0] for c in cl]
            weights = [c[2] for c in cl]
            labels  = [c[1] for c in cl]
            total_w = sum(weights)
            avg_p   = sum(p * w for p, w in zip(prices, weights)) / total_w
            dist    = abs(avg_p - current) / current * 100
            results.append({
                "price":      round(avg_p, 4),
                "score":      round(total_w, 1),
                "confluence": len(cl),
                "sources":    labels[:4],
                "dist_pct":   round(dist, 2),
            })

        results.sort(key=lambda x: (x["confluence"], x["score"]), reverse=True)
        return results[:3]

    buy_zones  = cluster_and_rank(_collect(is_buy=True))
    sell_zones = cluster_and_rank(_collect(is_buy=False))

    # ── Quality scoring ───────────────────────────────────────────────────────
    rsi        = momentum.get("rsi", 50)
    stg        = stage.get("stage", 0)
    macd_above = momentum.get("macd_above", False)
    hist_rising= momentum.get("hist_rising", False)
    cross_up   = momentum.get("macd_cross_up", False)
    cross_dn   = momentum.get("macd_cross_dn", False)

    def quality(zone, is_buy, opposing):
        entry  = zone["price"]
        stop   = round(entry - atr_val * 1.5, 4) if is_buy else round(entry + atr_val * 1.5, 4)

        above  = [z["price"] for z in opposing if z["price"] > entry]
        below  = [z["price"] for z in opposing if z["price"] < entry]
        if is_buy:
            target = round(min(above), 4) if above else round(entry + atr_val * 3, 4)
        else:
            target = round(max(below), 4) if below else round(entry - atr_val * 3, 4)

        risk   = abs(entry - stop)
        reward = abs(target - entry)
        rr     = round(reward / risk, 2) if risk > 0 else 0

        # Base stars from confluence (1-3)
        stars   = min(zone["confluence"], 3)
        factors = []

        # Adam Khoo: Trend Stage is the PRIMARY filter
        if is_buy:
            if stg == 2:
                stars += 1; factors.append("Stage 2 ↑")
            elif stg == 1:
                factors.append("Stage 1")
            elif stg == 3:
                stars -= 1; factors.append("⚠ Stage 3 Top")
            elif stg == 4:
                stars -= 1; factors.append("⚠ Stage 4 ↓")
        else:
            if stg == 4:
                stars += 1; factors.append("Stage 4 ↓")
            elif stg == 3:
                factors.append("Stage 3")
            elif stg == 2:
                factors.append("Stage 2")
            elif stg == 1:
                factors.append("Stage 1")

        # Adam Khoo: RSI confirmation
        if is_buy:
            if rsi <= 30:
                stars += 1; factors.append(f"RSI {rsi:.0f} Oversold ✓")
            elif rsi <= 45:
                factors.append(f"RSI {rsi:.0f}")
            elif rsi >= 70:
                factors.append(f"RSI {rsi:.0f} Overbought ✗")
        else:
            if rsi >= 70:
                stars += 1; factors.append(f"RSI {rsi:.0f} Overbought ✓")
            elif rsi >= 55:
                factors.append(f"RSI {rsi:.0f}")
            elif rsi <= 30:
                factors.append(f"RSI {rsi:.0f} Oversold ✗")

        # MACD confirmation
        if is_buy and cross_up:
            stars += 1; factors.append("MACD Cross ↑ ✓")
        elif is_buy and macd_above and hist_rising:
            factors.append("MACD Bullish")
        elif not is_buy and cross_dn:
            stars += 1; factors.append("MACD Cross ↓ ✓")
        elif not is_buy and not macd_above and not hist_rising:
            factors.append("MACD Bearish")

        # R:R quality (Jesse Livermore: always define risk before entry)
        if rr >= 3:
            factors.append(f"R:R {rr:.1f}:1 ✓")
        elif rr >= 2:
            factors.append(f"R:R {rr:.1f}:1")
        else:
            factors.append(f"R:R {rr:.1f}:1 ✗")

        return {
            "stars":   min(5, max(1, stars)),
            "factors": factors,
            "stop":    stop,
            "target":  target,
            "rr":      rr,
        }

    for z in buy_zones:
        z.update(quality(z, True, sell_zones))
    for z in sell_zones:
        z.update(quality(z, False, buy_zones))

    return {"buy": buy_zones, "sell": sell_zones}


# ══════════════════════════════════════════════════════════════════════════════
#  12. NEWS SENTIMENT  (keyword-based — no external API required)
#  Scores headlines the same way a buy-side analyst would scan morning news.
# ══════════════════════════════════════════════════════════════════════════════

# Financial-domain positive / negative keyword sets
_POS_WORDS = {
    "beat","beats","beating","exceed","exceeds","exceeded","record","growth",
    "profit","profits","upgrade","upgraded","buy","bullish","strong","surge",
    "surges","rally","rallies","gain","gains","higher","upside","outperform",
    "outperforms","positive","improve","improves","raises","raised","boost",
    "boosts","acquisition","dividend","expand","expansion","recovery",
    "opportunity","optimistic","breakthrough","innovation","win","wins",
    "partnership","deal","approved","approve","launch","launches","hired",
    "buyback","repurchase","guidance","raised guidance","above expectations",
    "record high","all-time high","momentum","accelerate","accelerating",
}
_NEG_WORDS = {
    "miss","misses","missed","decline","declines","declining","loss","losses",
    "downgrade","downgraded","sell","bearish","weak","fall","falls","drop",
    "drops","cut","cuts","lower","downside","underperform","concern","concerns",
    "risk","risks","warning","investigation","lawsuit","recall","layoff",
    "layoffs","restructure","debt","default","negative","disappointing",
    "below expectations","shortfall","problem","issue","delay","delayed",
    "withdrawn","suspend","suspended","probe","fine","penalty","cautious",
    "slowdown","contraction","headwinds","overvalued","bubble","crash","fear",
    "recession","inflation","sell-off","selloff","resign","resignation",
}

def news_sentiment(articles):
    """
    Keyword sentiment scan over all article titles + summaries.
    Returns score (−1 to +1), label, and per-article flags for UI colouring.
    """
    if not articles:
        return {"score": 0, "label": "Neutral", "pos": 0, "neg": 0, "articles": []}

    pos_total = neg_total = 0
    tagged = []

    for a in articles:
        text  = (a.get("title","") + " " + a.get("summary","")).lower()
        words = set(text.split())
        p = len(words & _POS_WORDS)
        n = len(words & _NEG_WORDS)
        pos_total += p
        neg_total += n
        if   p > n: tone = "positive"
        elif n > p: tone = "negative"
        else:       tone = "neutral"
        tagged.append({**a, "tone": tone})

    total = pos_total + neg_total
    if total == 0:
        score, label = 0, "Neutral"
    else:
        score = round((pos_total - neg_total) / total, 2)
        if   score >=  0.4: label = "Strongly Positive"
        elif score >=  0.15: label = "Positive"
        elif score <= -0.4: label = "Strongly Negative"
        elif score <= -0.15: label = "Negative"
        else:               label = "Neutral"

    return {"score": score, "label": label, "pos": pos_total,
            "neg": neg_total, "articles": tagged}


# ══════════════════════════════════════════════════════════════════════════════
#  13. ANALYST RATING  (combines ALL signals like a professional analyst)
#  Framework:  Adam Khoo (Stage/RSI/MACD) + Colin Seow (price action)
#              + Warren Buffett (fundamentals) + News sentiment
# ══════════════════════════════════════════════════════════════════════════════

# Symbols Shawn's own watchlist notes (ivy/analyst/watchlist.json) explicitly
# describe as sentiment/momentum plays with no real fundamental case ("trades
# almost entirely on news flow, not fundamentals" / "treat as a technical
# trade, not a buy-and-hold"). Folding a Buffett-style value score into the
# same composite used for quality names like BABA/MT would be inconsistent
# with how these are actually meant to be traded.
SENTIMENT_DRIVEN_SYMBOLS = {"BBAI", "DJT"}

def analyst_rating(meta, stage, momentum, signals, fundamentals, sentiment, patterns):
    """
    Produces a professional analyst rating (Strong Buy → Strong Sell),
    a numeric score (−10 to +10), ranked reasons, and a one-paragraph verdict.
    News sentiment is used to adjust the buy/sell signal quality and target.
    """
    score   = 0
    reasons = []
    sym     = meta["symbol"]
    price   = meta["price"]
    prev    = meta["prev_close"]
    chg_pct = (price - prev) / prev * 100 if prev else 0
    stg     = stage.get("stage", 0)
    rsi     = momentum.get("rsi", 50)
    sent_sc = sentiment.get("score", 0)

    # ── Adam Khoo: Stage Analysis (primary filter) ────────────────────────────
    if stg == 2:
        score += 3; reasons.append("Stage 2 Uptrend — bullish structure intact above SMA200")
    elif stg == 1:
        score += 1; reasons.append("Stage 1 Base — accumulation, watching for breakout above SMA200")
    elif stg == 3:
        score -= 2; reasons.append("Stage 3 Distribution — possible trend exhaustion at highs")
    elif stg == 4:
        score -= 3; reasons.append("Stage 4 Downtrend — price below SMA200, avoid long positions")

    # ── Adam Khoo: RSI zone ───────────────────────────────────────────────────
    if stg == 2 and 30 <= rsi <= 50:
        score += 2; reasons.append(f"RSI {rsi:.0f} — healthy pullback within uptrend (ideal entry)")
    elif rsi <= 30:
        score += 1; reasons.append(f"RSI {rsi:.0f} — oversold, high-probability bounce zone")
    elif rsi >= 70:
        score -= 1; reasons.append(f"RSI {rsi:.0f} — overbought, reduce position or wait for pullback")
    elif rsi >= 55 and stg == 2:
        reasons.append(f"RSI {rsi:.0f} — bullish momentum above midline")
    else:
        reasons.append(f"RSI {rsi:.0f} — below midline, caution")

    # ── MACD momentum ─────────────────────────────────────────────────────────
    vol_ratio = momentum.get("volume_ratio", 1.0)
    if momentum.get("macd_cross_up"):
        score += 2; reasons.append("MACD bullish crossover — momentum turning positive (buy signal)")
        # Volume confirmation: a crossover on above-average volume is a more
        # reliable signal; on thin volume it's more likely to fail/reverse.
        if vol_ratio >= 1.5:
            score += 1; reasons.append(f"Volume: {vol_ratio:.1f}x 20-day average — crossover backed by real buying pressure")
        elif vol_ratio < 0.7:
            score -= 1; reasons.append(f"Volume: {vol_ratio:.1f}x 20-day average — crossover lacks conviction, treat cautiously")
    elif momentum.get("macd_cross_dn"):
        score -= 2; reasons.append("MACD bearish crossover — momentum turning negative (sell signal)")
        if vol_ratio >= 1.5:
            score -= 1; reasons.append(f"Volume: {vol_ratio:.1f}x 20-day average — breakdown backed by real selling pressure")
        elif vol_ratio < 0.7:
            score += 1; reasons.append(f"Volume: {vol_ratio:.1f}x 20-day average — breakdown lacks conviction, treat cautiously")
    elif momentum.get("macd_above") and momentum.get("hist_rising"):
        score += 1; reasons.append("MACD histogram expanding — sustained buying pressure")
    elif not momentum.get("macd_above") and not momentum.get("hist_rising"):
        score -= 1; reasons.append("MACD histogram contracting — sustained selling pressure")

    # ── News Sentiment ────────────────────────────────────────────────────────
    if sent_sc >= 0.4:
        score += 2; reasons.append(f"News: {sentiment['label']} — macro tailwinds supporting price")
    elif sent_sc >= 0.15:
        score += 1; reasons.append(f"News: {sentiment['label']} — positive catalysts in play")
    elif sent_sc <= -0.4:
        score -= 2; reasons.append(f"News: {sentiment['label']} — negative catalysts creating headwinds")
    elif sent_sc <= -0.15:
        score -= 1; reasons.append(f"News: {sentiment['label']} — monitor for further downside")
    else:
        reasons.append("News: Neutral — no significant catalyst detected")

    # ── Warren Buffett: Fundamentals ─────────────────────────────────────────
    val_score = (fundamentals or {}).get("value_score", 0)
    if sym in SENTIMENT_DRIVEN_SYMBOLS:
        reasons.append("Fundamentals: N/A — sentiment/momentum play, trade technicals only")
    elif val_score >= 4:
        score += 2
        vr = ", ".join((fundamentals or {}).get("value_reasons", []))
        reasons.append(f"Fundamentals: Buffett-grade quality (score {val_score}/5) — {vr}")
    elif val_score >= 2:
        score += 1; reasons.append(f"Fundamentals: Adequate quality (score {val_score}/5)")
    elif fundamentals and val_score == 0:
        score -= 1; reasons.append("Fundamentals: Below Buffett threshold — trade technicals only")

    # ── Colin Seow: Candlestick confirmation ─────────────────────────────────
    for p in patterns[:1]:
        if p["type"] == "bullish":
            score += 1; reasons.append(f"Pattern: {p['name']} — bullish reversal confirms entry")
        elif p["type"] == "bearish":
            score -= 1; reasons.append(f"Pattern: {p['name']} — bearish signal warns of reversal")

    # ── Best trade zone quality ───────────────────────────────────────────────
    buy_zones  = signals.get("buy",  [])
    sell_zones = signals.get("sell", [])
    best_buy   = max(buy_zones,  key=lambda z: z.get("stars", 0), default=None)
    best_sell  = max(sell_zones, key=lambda z: z.get("stars", 0), default=None)

    # ── Position sizing: fixed-fractional risk model ──────────────────────────
    # Risking a fixed % of total portfolio per trade, sized off the stop
    # distance, is standard risk management (Van Tharp / Jesse Livermore
    # "always define risk before entry") that this tool previously gave a
    # stop/target for but never translated into how much to actually buy.
    # Capped so a very tight stop doesn't suggest an absurd concentration.
    RISK_PER_TRADE_PCT = 1.0
    MAX_POSITION_PCT   = 20.0
    suggested_position_pct = None
    if best_buy and best_buy.get("price") and best_buy.get("stop"):
        entry = best_buy["price"]
        stop  = best_buy["stop"]
        risk_pct_of_price = abs(entry - stop) / entry * 100
        if risk_pct_of_price > 0:
            suggested_position_pct = round(
                min(RISK_PER_TRADE_PCT / risk_pct_of_price * 100, MAX_POSITION_PCT), 1
            )

    # News tilts the analyst's own verdict below, but must NOT mutate the zone
    # objects in `signals` — those are the same objects rendered in the Trade
    # Signals panel (and reused by the screener/portfolio-alert jobs), and a
    # silent one-off star bump there made otherwise-equal zones look
    # inconsistently rated for no visible reason.
    best_buy_stars  = min(5, best_buy.get("stars", 1)  + 1) if best_buy  and sent_sc >=  0.15 else (best_buy.get("stars", 1)  if best_buy  else 0)
    best_sell_stars = min(5, best_sell.get("stars", 1) + 1) if best_sell and sent_sc <= -0.15 else (best_sell.get("stars", 1) if best_sell else 0)

    # ── Rating thresholds ─────────────────────────────────────────────────────
    if   score >= 7:  rating, color = "STRONG BUY",  "#22c55e"
    elif score >= 4:  rating, color = "BUY",          "#3fb950"
    elif score >= 1:  rating, color = "MILD BUY",     "#86efac"
    elif score >= -1: rating, color = "HOLD",         "#e3b341"
    elif score >= -4: rating, color = "SELL",         "#f85149"
    else:             rating, color = "STRONG SELL",  "#dc2626"

    # ── Analyst verdict paragraph ─────────────────────────────────────────────
    verdict_parts = []

    # Opening: trend situation
    if stg == 2:
        verdict_parts.append(f"{sym} is in a Stage 2 uptrend with price above the 200-day SMA — the primary bullish filter passes.")
    elif stg == 4:
        verdict_parts.append(f"{sym} is in a Stage 4 downtrend trading below SMA200 — longs are high risk until structure recovers.")
    elif stg == 3:
        verdict_parts.append(f"{sym} shows Stage 3 distribution characteristics — the trend may be topping after an extended run.")
    else:
        verdict_parts.append(f"{sym} is building a Stage 1 base — patience required for a confirmed Stage 2 breakout.")

    # RSI situation
    if stg == 2 and 30 <= rsi <= 50:
        verdict_parts.append(f"RSI {rsi:.0f} signals a pullback within the uptrend — Adam Khoo's ideal low-risk entry window.")
    elif rsi <= 30:
        verdict_parts.append(f"RSI {rsi:.0f} is deeply oversold — historically this is a high-probability bounce zone.")
    elif rsi >= 70:
        verdict_parts.append(f"RSI {rsi:.0f} is overbought — chasing here increases risk; wait for a pullback to support.")

    # Best buy zone
    if best_buy:
        verdict_parts.append(
            f"Best buy zone is {best_buy['price']:.2f} ({best_buy['confluence']}× confluence, {best_buy_stars}★): "
            f"stop {best_buy.get('stop',0):.2f} · target {best_buy.get('target',0):.2f} · R:R {best_buy.get('rr',0):.1f}:1."
        )

    # News context
    if sent_sc >= 0.15:
        verdict_parts.append(f"News sentiment is {sentiment['label'].lower()} — positive catalysts provide additional tailwind.")
    elif sent_sc <= -0.15:
        verdict_parts.append(f"News sentiment is {sentiment['label'].lower()} — negative headlines create headwinds; tighten stops.")

    # Fundamental summary
    if sym in SENTIMENT_DRIVEN_SYMBOLS:
        verdict_parts.append("This is a sentiment/momentum name, not a fundamentals play — treat purely as a technical trade with strict risk management.")
    elif val_score >= 4:
        verdict_parts.append(f"Fundamentally this is a Buffett-quality business ({val_score}/5 value score) — suitable for longer-term holds.")
    elif val_score <= 1 and fundamentals:
        verdict_parts.append("Fundamentals are weak — treat this as a purely technical trade with strict risk management.")

    # Position sizing
    if suggested_position_pct is not None:
        verdict_parts.append(
            f"Suggested position size: ~{suggested_position_pct:.1f}% of portfolio "
            f"(risking {RISK_PER_TRADE_PCT:.0f}% of total capital to the stop) — adjust for your own risk tolerance."
        )

    # Closing
    if rating in ("STRONG BUY", "BUY"):
        verdict_parts.append("Overall: high-probability long setup — manage risk with defined stop loss.")
    elif rating in ("STRONG SELL", "SELL"):
        verdict_parts.append("Overall: avoid long positions; existing holders should consider reducing exposure.")
    else:
        verdict_parts.append("Overall: mixed signals — wait for clearer confirmation before committing capital.")

    return {
        "rating":  rating,
        "score":   score,
        "color":   color,
        "reasons": reasons[:7],
        "verdict": " ".join(verdict_parts),
        "target":  best_buy.get("target")  if best_buy  else None,
        "stop":    best_buy.get("stop")    if best_buy  else None,
        "best_buy_price":  best_buy.get("price")  if best_buy  else None,
        "best_sell_price": best_sell.get("price") if best_sell else None,
        "suggested_position_pct": suggested_position_pct,
        "risk_per_trade_pct":     RISK_PER_TRADE_PCT,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  14. NEWS  (yfinance built-in — no extra API key required)
# ══════════════════════════════════════════════════════════════════════════════

def get_news(ticker_obj):
    """
    Fetches recent news articles for the ticker via yfinance.
    Handles both old (flat dict) and new (nested 'content') yfinance formats.
    Returns up to 8 articles with title, publisher, link, and timestamp.
    """
    try:
        raw = ticker_obj.news
        if not raw:
            return []
        articles = []
        for item in raw[:10]:
            # yfinance ≥0.2.50 wraps fields in a "content" sub-dict
            src = item.get("content", item)

            title     = src.get("title") or item.get("title", "")
            publisher = (src.get("provider", {}).get("displayName")
                         or src.get("publisher")
                         or item.get("publisher", ""))
            summary   = (src.get("summary") or item.get("summary", ""))[:200]

            # Link resolution (varies by yfinance version)
            link = (src.get("canonicalUrl", {}).get("url")
                    or src.get("clickThroughUrl", {}).get("url")
                    or src.get("link")
                    or item.get("link", ""))

            # Timestamp (unix seconds)
            pub_ts = item.get("providerPublishTime", 0)
            if not pub_ts:
                try:
                    import datetime
                    raw_dt = src.get("pubDate", "")
                    if raw_dt:
                        dt = datetime.datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                        pub_ts = int(dt.timestamp())
                except Exception:
                    pub_ts = 0

            if title:
                articles.append({
                    "title":     title[:130],
                    "publisher": publisher,
                    "link":      link,
                    "time":      pub_ts,
                    "summary":   summary,
                })
        return articles[:8]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  13. MARKET CONTEXT  (why is the stock moving today?)
# ══════════════════════════════════════════════════════════════════════════════

def market_context(meta, stage, momentum, patterns):
    """
    Generates plain-English bullets explaining the current price action.
    Combines today's price move with trend stage, momentum, and patterns.
    """
    bullets = []
    price   = meta["price"]
    prev    = meta["prev_close"]
    chg_pct = (price - prev) / prev * 100 if prev else 0

    # Today's price move
    if   chg_pct >  3:  bullets.append(f"Strong surge +{chg_pct:.1f}% — high-momentum buying day")
    elif chg_pct >  0.5: bullets.append(f"Rising +{chg_pct:.1f}% today")
    elif chg_pct < -3:  bullets.append(f"Heavy selling — down {abs(chg_pct):.1f}% today")
    elif chg_pct < -0.5: bullets.append(f"Declining {chg_pct:.1f}% today")
    else:                bullets.append(f"Flat ({chg_pct:+.2f}%) — price consolidating")

    # Trend stage context (Adam Khoo Stage Analysis)
    stg = stage.get("stage", 0)
    if   stg == 2: bullets.append("Trend: Stage 2 Uptrend — bullish structure intact above SMA200")
    elif stg == 4: bullets.append("Trend: Stage 4 Downtrend — avoid longs, bearish below SMA200")
    elif stg == 3: bullets.append("Trend: Stage 3 Distribution — possible top, proceed cautiously")
    elif stg == 1: bullets.append("Trend: Stage 1 Base — accumulation phase, watching for breakout")

    # RSI zone
    rsi = momentum.get("rsi", 50)
    if   rsi >= 70: bullets.append(f"RSI {rsi:.0f} — overbought territory, watch for pullback")
    elif rsi <= 30: bullets.append(f"RSI {rsi:.0f} — oversold zone, potential bounce opportunity")
    elif rsi >= 55: bullets.append(f"RSI {rsi:.0f} — bullish momentum above midline")
    else:           bullets.append(f"RSI {rsi:.0f} — below midline, bearish momentum")

    # MACD signal
    if   momentum.get("macd_cross_up"): bullets.append("MACD bullish crossover — momentum turning positive")
    elif momentum.get("macd_cross_dn"): bullets.append("MACD bearish crossover — momentum turning negative")
    elif momentum.get("macd_above") and momentum.get("hist_rising"):
        bullets.append("MACD histogram expanding — buying pressure increasing")
    elif not momentum.get("macd_above") and not momentum.get("hist_rising"):
        bullets.append("MACD histogram contracting — selling pressure increasing")

    # Latest candlestick pattern
    for p in patterns[:1]:
        if   p["type"] == "bullish": bullets.append(f"Candle: {p['name']} — bullish reversal signal")
        elif p["type"] == "bearish": bullets.append(f"Candle: {p['name']} — bearish reversal signal")
        else:                        bullets.append(f"Candle: {p['name']} — market indecision")

    return bullets[:6]


# ══════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stock")
def get_stock():
    symbol   = request.args.get("symbol", "AAPL").upper()
    period   = request.args.get("period",   "1y")
    interval = request.args.get("interval", "1d")

    try:
        ticker = yf.Ticker(symbol)
        df     = ticker.history(period=period, interval=interval)

        if df.empty:
            return jsonify({"error": f"No data found for '{symbol}'"}), 404

        df = df.reset_index()
        df.columns = [c.replace(" ", "_") for c in df.columns]
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)

        if len(df) < 15:
            return jsonify({"error": "Too few candles for reliable analysis — choose a longer period."}), 400

        # ── OHLCV for chart ───────────────────────────────────────────────────
        candles = []
        for _, row in df.iterrows():
            ts = row["Datetime"] if "Datetime" in row else row["Date"]
            candles.append({
                "time":   int(pd.Timestamp(ts).timestamp()),
                "open":   round(float(row["Open"]),  4),
                "high":   round(float(row["High"]),  4),
                "low":    round(float(row["Low"]),   4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            })

        # ── Moving averages as a time-series for the chart ───────────────────
        c = df["Close"]
        ma_series = {}
        for period_ma, name in [(20, "SMA20"), (50, "SMA50"), (200, "SMA200")]:
            if len(c) >= period_ma:
                vals = c.rolling(period_ma).mean()
                series = []
                for i, (_, row) in enumerate(df.iterrows()):
                    v = vals.iloc[i]
                    if not np.isnan(v):
                        ts = row["Datetime"] if "Datetime" in row else row["Date"]
                        series.append({"time": int(pd.Timestamp(ts).timestamp()),
                                       "value": round(float(v), 4)})
                if series:
                    ma_series[name] = series

        # ── All S/R analyses ─────────────────────────────────────────────────
        atr_val  = calc_atr(df)
        pp       = pivot_points(df, interval)
        fractals = fractal_sr(df)
        vp       = volume_profile(df)
        mas      = moving_averages(df)
        rounds   = round_number_levels(float(df["Close"].iloc[-1]))
        periods  = period_levels(df)
        momentum = momentum_indicators(df)
        stage    = trend_stage(df, mas)
        patterns = candlestick_patterns(df, atr_val)
        signals  = trade_signals(df, pp, fractals, vp, mas, rounds, periods, momentum, stage)

        info = ticker.fast_info
        meta = {
            "symbol":     symbol,
            "price":      round(float(info.last_price), 4),
            "prev_close": round(float(info.previous_close), 4),
            "currency":   getattr(info, "currency", "USD"),
            "atr":        round(atr_val, 4),
        }

        try:
            fundamentals = get_fundamentals(ticker)
        except Exception:
            fundamentals = {}

        try:
            news = get_news(ticker)
        except Exception:
            news = []

        sentiment = news_sentiment(news)
        context   = market_context(meta, stage, momentum, patterns)
        analyst   = analyst_rating(meta, stage, momentum, signals,
                                   fundamentals, sentiment, patterns)

        return jsonify({
            "meta":            meta,
            "candles":         candles,
            "ma_series":       ma_series,
            "pivot":           pp,
            "fractals":        fractals,
            "volume_profile":  vp,
            "moving_averages": mas,
            "round_numbers":   rounds,
            "period_levels":   periods,
            "momentum":        momentum,
            "stage":           stage,
            "patterns":        patterns,
            "signals":         signals,
            "fundamentals":    fundamentals,
            "news":            sentiment.get("articles", news),
            "sentiment":       sentiment,
            "context":         context,
            "analyst":         analyst,
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


def analyze_symbol(sym, period="1y", interval="1d"):
    """
    Full single-symbol analysis pipeline: fetches data via yfinance,
    computes all technical/fundamental signals, and returns a condensed
    analyst rating dict.

    Default period is 1y, not 3mo -- SMA200/Stage Analysis needs ~200
    daily bars; a shorter period silently returns stage=0 "N/A", which
    used to disable the model's own primary trend filter by default.

    Standalone (no Flask request context needed) so it can be reused by
    the /api/screen route AND by external scripts — e.g. the scheduled
    portfolio_alert.py job — without requiring the web server to be running.

    Newer yfinance (≥0.2.51) uses curl_cffi internally — do NOT pass a
    custom requests.Session.
    """
    try:
        ticker = yf.Ticker(sym)
        df     = ticker.history(period=period, interval=interval,
                                auto_adjust=True, repair=False)

        if df is None or df.empty:
            return {"symbol": sym, "error": "Yahoo Finance returned no data — check symbol"}

        df = df.reset_index()
        df.columns = [c.replace(" ", "_") for c in df.columns]
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)

        if len(df) < 15:
            return {"symbol": sym, "error": f"Only {len(df)} bars — use a longer period"}

        # ── Technical analysis ─────────────────────────────────────────────
        atr_val  = calc_atr(df)
        pp       = pivot_points(df, interval)
        fractals = fractal_sr(df)
        vp       = volume_profile(df)
        mas      = moving_averages(df)
        rounds   = round_number_levels(float(df["Close"].iloc[-1]))
        pers     = period_levels(df)
        mom      = momentum_indicators(df)
        stg      = trend_stage(df, mas)
        pats     = candlestick_patterns(df, atr_val)
        sigs     = trade_signals(df, pp, fractals, vp, mas, rounds, pers, mom, stg)

        # ── Price meta — fall back to df if fast_info is unreliable ────────
        last_close = round(float(df["Close"].iloc[-1]), 4)
        prev_close = round(float(df["Close"].iloc[-2]), 4) if len(df) >= 2 else last_close
        try:
            fi = ticker.fast_info
            lp = fi.last_price
            pc = fi.previous_close
            price    = round(float(lp), 4) if lp and not np.isnan(float(lp)) else last_close
            prev     = round(float(pc), 4) if pc and not np.isnan(float(pc)) else prev_close
            currency = getattr(fi, "currency", "USD") or "USD"
        except Exception:
            price, prev, currency = last_close, prev_close, "USD"

        chg_pct = round((price - prev) / prev * 100, 2) if prev else 0
        meta    = {"symbol": sym, "price": price, "prev_close": prev,
                   "currency": currency, "atr": round(atr_val, 4)}

        # ── News + fundamentals ────────────────────────────────────────────
        try:   news = get_news(ticker)
        except: news = []

        sentiment    = news_sentiment(news)
        fundamentals = {}
        try:   fundamentals = get_fundamentals(ticker)
        except: pass

        arating  = analyst_rating(meta, stg, mom, sigs, fundamentals, sentiment, pats)
        best_buy = (max(sigs["buy"], key=lambda z: z.get("stars", 0))
                    if sigs["buy"] else None)

        return {
            "symbol":      sym,
            "name":        fundamentals.get("name"),
            "price":       price,
            "change":      chg_pct,
            "currency":    currency,
            "rating":      arating["rating"],
            "score":       arating["score"],
            "color":       arating["color"],
            "stage":       stg["stage"],
            "stage_label": stg["label"],
            "stage_color": stg["color"],
            "rsi":         mom["rsi"],
            "rsi_zone":    mom["rsi_zone"],
            "reason":      arating["reasons"][0] if arating["reasons"] else "",
            "entry":       arating.get("best_buy_price"),
            "target":      arating.get("target"),
            "stop":        arating.get("stop"),
            "rr":          best_buy.get("rr", 0) if best_buy else 0,
            "stars":       best_buy.get("stars", 0) if best_buy else 0,
            "suggested_position_pct": arating.get("suggested_position_pct"),
            "risk_per_trade_pct":     arating.get("risk_per_trade_pct"),
            "sentiment":   sentiment["label"],
            "sent_color":  ("#3fb950" if sentiment["score"] >= 0.15
                            else "#f85149" if sentiment["score"] <= -0.15
                            else "#e3b341"),
            "verdict":     (arating.get("verdict", "") or "")[:200],
        }
    except Exception as exc:
        return {"symbol": sym, "error": f"{type(exc).__name__}: {str(exc)[:100]}"}


@app.route("/api/screen")
def screen_stocks():
    """
    Screener endpoint — analyses multiple tickers (via analyze_symbol)
    and returns condensed analyst ratings, sorted strongest to weakest.

    Workers capped at 3 to stay under Yahoo Finance rate limits.
    """
    symbols_raw = request.args.get("symbols", "")
    period      = request.args.get("period",   "1y")
    interval    = request.args.get("interval", "1d")

    symbols = [s.strip().upper()
               for s in symbols_raw.replace("\n", ",").split(",")
               if s.strip() and len(s.strip()) <= 12][:25]

    if not symbols:
        return jsonify([])

    results = []
    # max_workers=3 — higher concurrency triggers Yahoo Finance rate limiting
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(analyze_symbol, s, period, interval): s for s in symbols}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda x: x.get("score", -99), reverse=True)
    return jsonify(results)


@app.route("/api/portfolio")
def portfolio_view():
    """
    Portfolio-level view -- something no single-symbol page can show:
    current rating + suggested position size for every held symbol
    (alerts.PORTFOLIO), plus a pairwise daily-return correlation matrix
    to surface hidden concentration risk (several "different" holdings
    that actually move together, e.g. multiple high-beta momentum names).
    """
    symbols = alerts.PORTFOLIO

    holdings = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(analyze_symbol, s, "1y"): s for s in symbols}
        for fut in as_completed(futures):
            holdings.append(fut.result())
    holdings.sort(key=lambda x: x.get("score", -99), reverse=True)

    def _fetch_returns(sym):
        try:
            df = yf.Ticker(sym).history(period="1y", interval="1d")
            if df is None or df.empty:
                return sym, None
            r = df["Close"].pct_change().dropna()
            # Portfolio spans HKEX/Nasdaq Stockholm/NYSE -- each comes back
            # tz-aware in its own exchange's local timezone, so the same
            # calendar day won't align across symbols unless normalized.
            r.index = pd.to_datetime(r.index.date)
            return sym, r
        except Exception:
            return sym, None

    returns = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for sym, r in pool.map(_fetch_returns, symbols):
            if r is not None and len(r) > 5:
                returns[sym] = r

    corr_matrix, avg_correlation, high_correlation_pairs = {}, {}, []
    if len(returns) >= 2:
        # No blanket dropna() here -- .corr() does pairwise-complete
        # correlation per column pair by default, which is the correct way
        # to handle markets whose holiday calendars don't fully overlap.
        rdf  = pd.DataFrame(returns)
        corr = rdf.corr()
        # NaN (too few overlapping trading days for a given pair) can't
        # serialize to valid JSON -- surface as null rather than "NaN".
        corr_matrix = {a: {b: (round(float(corr.loc[a, b]), 2) if not np.isnan(corr.loc[a, b]) else None)
                            for b in corr.columns} for a in corr.index}
        for sym in corr.index:
            others = [corr.loc[sym, b] for b in corr.columns if b != sym and not np.isnan(corr.loc[sym, b])]
            avg_correlation[sym] = round(float(sum(others) / len(others)), 2) if others else None

        seen = set()
        for a in corr.index:
            for b in corr.columns:
                if a == b or (b, a) in seen:
                    continue
                seen.add((a, b))
                c = float(corr.loc[a, b])
                if not np.isnan(c) and c >= 0.7:
                    high_correlation_pairs.append({"a": a, "b": b, "correlation": round(c, 2)})
        high_correlation_pairs.sort(key=lambda x: x["correlation"], reverse=True)

    return jsonify({
        "holdings":               holdings,
        "correlation_matrix":     corr_matrix,
        "avg_correlation":        avg_correlation,
        "high_correlation_pairs": high_correlation_pairs,
    })


@app.route("/api/search")
def search_ticker():
    q = request.args.get("q", "")
    if not q:
        return jsonify([])
    try:
        results = yf.Search(q, max_results=8)
        quotes  = results.quotes if hasattr(results, "quotes") else []
        return jsonify([
            {"symbol": r.get("symbol", ""),
             "name":   r.get("shortname", r.get("longname", ""))}
            for r in quotes
        ])
    except Exception:
        return jsonify([])


@app.route("/api/alert-check")
def alert_check():
    """Manual trigger for testing the Telegram alert job — protected since
    the site is fully public and this pushes a message to a real phone."""
    key = request.args.get("key", "")
    expected = os.environ.get("ALERT_TEST_KEY")
    if not expected or key != expected:
        return jsonify({"error": "forbidden"}), 403
    alerts.check_and_alert()
    return jsonify({"ok": True})


@app.route("/api/subscribers")
def subscribers_list_route():
    """Read-only subscriber list for Shawn's own use — protected since the
    site is fully public. Reuses ALERT_TEST_KEY (same secret as /api/alert-check)."""
    key = request.args.get("key", "")
    expected = os.environ.get("ALERT_TEST_KEY")
    if not expected or key != expected:
        return jsonify({"error": "forbidden"}), 403
    rows = subscribers.get_all_subscribers()
    return jsonify({
        "total": len(rows),
        "confirmed": sum(1 for _, c, _ in rows if c),
        "pending": sum(1 for _, c, _ in rows if not c),
        "subscribers": [
            {"email": e, "confirmed": c, "created_at": t.isoformat() if t else None}
            for e, c, t in rows
        ],
    })


@app.route("/api/subscribe", methods=["POST"])
@limiter.limit("5 per hour")
def subscribe_route():
    email = (request.json or {}).get("email", "") if request.is_json else request.form.get("email", "")
    ok, message = subscribers.subscribe(email)
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"ok": False, "message": "Too many attempts — please try again later."}), 429


@app.route("/api/subscribe/confirm")
def subscribe_confirm_route():
    email = subscribers.confirm(request.args.get("token", ""))
    if email:
        return "<h2>Subscribed!</h2><p>You'll get an email whenever a STRONG BUY signal fires on getChartPulse's watchlist. You can close this tab.</p>"
    return "<h2>Invalid or expired link.</h2>", 400


@app.route("/api/subscribe/unsubscribe")
def subscribe_unsubscribe_route():
    email = subscribers.unsubscribe(request.args.get("token", ""))
    if email:
        return "<h2>Unsubscribed.</h2><p>You won't receive any more getChartPulse alerts.</p>"
    return "<h2>Invalid or already removed.</h2>", 400


import alerts        # noqa: E402  (imports analyze_symbol from this module)
import subscribers   # noqa: E402
subscribers.init_db()
alerts.start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
