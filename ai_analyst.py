# ============================================================
# File: ai_analyst.py
# Date: 2026-10-04
# Author: Developer Team, getChartPulse
# Task: Independent AI-generated stock verdict (Claude Opus 5.5),
#       shown alongside the existing rule-based Composite Analyst
#       (analyst_rating() in app.py) for side-by-side comparison.
#       Unlike the Composite Analyst, which channels named trading
#       styles (Adam Khoo / Colin Seow / Warren Buffett) through
#       deterministic scoring rules, this gives Claude the same
#       underlying computed signals and asks it to reason as a
#       neutral, logical analyst in its own words.
#
#       Cached once per calendar day per symbol in Postgres, and
#       capped at a configurable number of unique symbols/day, so
#       cost stays bounded regardless of how much traffic the site
#       gets -- a real concern since this is a public site, not
#       just Shawn's own use.
#
#       Gated at the call site (app.py) to logged-in subscribers
#       only, as a second layer of cost protection on top of the
#       cache and the daily cap.
# ============================================================

import json
import os
from datetime import datetime, timezone

import anthropic
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Configurable without a redeploy -- change this Render env var to
# raise/lower the worst-case daily spend without touching code.
DAILY_CAP = int(os.environ.get("AI_ANALYST_DAILY_CAP", "20"))

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

SYSTEM_PROMPT = (
    "You are a professional, accurate, logical prediction analyst. You reason strictly "
    "from the data given -- technical structure, momentum, fundamentals, sentiment, and "
    "chart patterns -- using clear, evidence-based logic. You do not invoke named investors "
    "or trading personalities, you do not hedge with vague language, and you do not claim "
    "certainty you don't have. State your reasoning plainly, weigh conflicting signals "
    "explicitly when they disagree, and give one clear rating. You know that no analysis "
    "can reliably predict short-term price moves -- your job is to assess the quality and "
    "balance of the current evidence, not to promise an outcome."
)

RATING_COLORS = {
    "STRONG BUY": "#22c55e", "BUY": "#3fb950", "MILD BUY": "#86efac",
    "HOLD": "#e3b341", "SELL": "#f85149", "STRONG SELL": "#dc2626",
}


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("ai_analyst: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_analyst_cache (
                    symbol     TEXT NOT NULL,
                    cache_date DATE NOT NULL,
                    rating     TEXT,
                    score      INTEGER,
                    verdict    TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (symbol, cache_date)
                )
            """)
        conn.commit()
    print("ai_analyst: table ready")


def _today():
    return datetime.now(timezone.utc).date()


def _get_cached(symbol):
    if not DATABASE_URL:
        return None
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rating, score, verdict FROM ai_analyst_cache "
                "WHERE symbol = %s AND cache_date = %s",
                (symbol, _today()),
            )
            row = cur.fetchone()
    if not row:
        return None
    rating, score, verdict = row
    return {
        "rating": rating, "score": score, "verdict": verdict,
        "color": RATING_COLORS.get(rating, "#8b949e"), "cached": True,
    }


def _count_today():
    if not DATABASE_URL:
        return 0
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ai_analyst_cache WHERE cache_date = %s",
                (_today(),),
            )
            return cur.fetchone()[0]


def _save(symbol, rating, score, verdict):
    if not DATABASE_URL:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_analyst_cache (symbol, cache_date, rating, score, verdict)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (symbol, cache_date) DO UPDATE SET
                    rating = EXCLUDED.rating, score = EXCLUDED.score, verdict = EXCLUDED.verdict
            """, (symbol, _today(), rating, score, verdict))
        conn.commit()


def get_ai_verdict(symbol, meta, stage, momentum, fundamentals, sentiment, patterns, signals):
    """Returns an independent Claude-generated verdict for `symbol`, cached once per
    calendar day. Returns None if the feature isn't configured, the daily cap is
    reached, or the API call fails/refuses -- callers must treat None as "no AI
    panel this time", never as an error to surface, since the rule-based Composite
    Analyst already covers the page without it."""
    if not _client or not DATABASE_URL:
        return None

    cached = _get_cached(symbol)
    if cached:
        return cached

    if _count_today() >= DAILY_CAP:
        return None

    best_buy = max(signals.get("buy", []), key=lambda z: z.get("stars", 0), default=None)
    best_sell = max(signals.get("sell", []), key=lambda z: z.get("stars", 0), default=None)
    data = {
        "symbol": symbol,
        "price": meta.get("price"),
        "prev_close": meta.get("prev_close"),
        "trend_stage": stage.get("stage"),
        "rsi": momentum.get("rsi"),
        "macd_cross_up": momentum.get("macd_cross_up"),
        "macd_cross_dn": momentum.get("macd_cross_dn"),
        "volume_vs_20day_avg": momentum.get("volume_ratio"),
        "fundamentals_value_score_0to5": (fundamentals or {}).get("value_score"),
        "fundamentals_reasons": (fundamentals or {}).get("value_reasons"),
        "news_sentiment_score_-1to1": sentiment.get("score"),
        "news_sentiment_label": sentiment.get("label"),
        "most_recent_candlestick_pattern": patterns[0]["name"] if patterns else None,
        "pattern_type": patterns[0]["type"] if patterns else None,
        "best_buy_zone": best_buy,
        "best_sell_zone": best_sell,
    }

    prompt = (
        f"Analyze {symbol} using only this data:\n{json.dumps(data, default=str)}\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no other text), exactly "
        'this shape: {"rating": one of "STRONG BUY" | "BUY" | "MILD BUY" | "HOLD" | '
        '"SELL" | "STRONG SELL", "score": integer from -10 to 10, "verdict": a 2-4 '
        "sentence plain-language paragraph explaining your reasoning for a retail "
        "investor, explicitly weighing any signals that disagree with each other}"
    )

    try:
        response = _client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            print(f"ai_analyst: refused for {symbol} (category={category})")
            return None

        text = next((b.text for b in response.content if b.type == "text"), "")
        parsed = json.loads(text)
        rating = parsed["rating"]
        score = int(parsed["score"])
        verdict = parsed["verdict"]
    except Exception as e:
        print(f"ai_analyst: Claude call failed for {symbol}: {type(e).__name__}: {e}")
        return None

    _save(symbol, rating, score, verdict)
    return {
        "rating": rating, "score": score, "verdict": verdict,
        "color": RATING_COLORS.get(rating, "#8b949e"), "cached": False,
    }
