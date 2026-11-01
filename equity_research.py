# ============================================================
# File: equity_research.py
# Date: 2026-11-02
# Author: Developer Team, getChartPulse
# Task: On-demand "Equity Research Report" for a symbol over a
#       chosen investment horizon (1y / 3y / 5y), written by Claude
#       Opus 5.5 acting as a senior equity research analyst, with
#       live web search so revenue mix, segments and customer
#       concentration come from recent filings rather than stale
#       training data.
#
#       Unlike ai_analyst.py (a short technical verdict computed from
#       the page's own signals), this is a fundamental write-up:
#       revenue streams, segments, customer concentration, moat,
#       growth drivers, 3 opportunities + 3 risks (each with
#       evidence / metrics to monitor / warning signs), and open
#       questions. It deliberately gives NO buy/sell rating.
#
#       Second report type, "financial": a financial-strength review
#       of the last 3 fiscal years + comparable YTD (revenue, growth,
#       op margin, OCF, capex, FCF, debt, cash, diluted shares) with a
#       source id on every figure, trend / cash-flow quality / debt /
#       dilution / working-capital analysis, a peer benchmark, and
#       deterioration flags.
#
#       A report takes 1-3 minutes (several web searches), far past
#       gunicorn's 30s worker timeout on a single sync worker, so it
#       runs in a background thread and the page polls for it.
#       In-memory job tracking is fine for the same reason
#       Flask-Limiter's in-memory store is: Render runs one worker.
#
#       Cost controls, same layering as ai_analyst.py:
#         1. login-gated at the call site (app.py)
#         2. cached per (symbol, horizon) for CACHE_DAYS
#         3. capped at DAILY_CAP fresh reports per day site-wide
# ============================================================

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import anthropic
import psycopg2
from psycopg2.extras import Json

DATABASE_URL = os.environ.get("DATABASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Configurable without a redeploy. Each report runs several web searches
# and a long output, so it costs roughly 10-30x an AI Analyst verdict --
# the default cap is deliberately lower than AI_ANALYST_DAILY_CAP.
DAILY_CAP = int(os.environ.get("EQUITY_RESEARCH_DAILY_CAP", "5"))
# Fundamentals move quarterly, not daily -- a week-old report is still
# current, and this keeps repeat views of popular symbols free.
CACHE_DAYS = int(os.environ.get("EQUITY_RESEARCH_CACHE_DAYS", "7"))

HORIZONS = {"1y": "1-year", "3y": "3-year", "5y": "5-year"}
# Two report types share one pipeline and one cache table. The table's
# `horizon` column is the cache key: "1y"/"3y"/"5y" for a business report,
# FIN_KEY for a financial-strength report (which has no horizon).
REPORT_TYPES = ("business", "financial")
FIN_KEY = "fin"


def cache_key(report, horizon):
    return FIN_KEY if report == "financial" else horizon

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# (symbol, cache_key) -> "pending" | "failed". Only tracks jobs started by
# this process; finished reports live in Postgres.
_jobs = {}
_jobs_lock = threading.Lock()

SYSTEM_PROMPT = (
    "You are a senior equity research analyst. You write rigorous, evidence-based "
    "fundamental research for retail investors. Use web search to ground every "
    "material claim in the company's most recent filings (10-K/10-Q/20-F/annual "
    "report), earnings calls and investor presentations, and reputable financial "
    "press. Always name the fiscal period a figure comes from. If a figure is not "
    "disclosed or you cannot verify it, say so plainly instead of estimating it as "
    "fact. Weigh bull and bear evidence evenly. You do not give buy/sell/hold "
    "ratings or price targets -- your job is to show what matters and what to "
    "watch, not to tell anyone what to do."
)

OUTPUT_SHAPE = """{
  "company": "full company name",
  "as_of": "latest reporting period your figures come from, e.g. 'FY2026 Q1 (Sep 2026)'",
  "summary": "3-5 sentence overview of the business and the investment debate over this horizon",
  "revenue_streams": [{"name": "", "share": "% of revenue or 'not disclosed'", "trend": "growing | stable | declining", "note": ""}],
  "segments": [{"name": "", "description": "", "revenue": "latest figure with period", "growth_or_margin": ""}],
  "customer_concentration": {"level": "High | Moderate | Low | Not disclosed", "assessment": "", "details": [""]},
  "competitive_advantages": [{"advantage": "", "strength": "Strong | Moderate | Weak", "evidence": ""}],
  "growth_drivers": [{"driver": "", "impact": "High | Medium | Low", "timing": ""}],
  "opportunities": [{"factor": "", "evidence": "", "metrics": "", "warning_signs": ""}],
  "risks": [{"factor": "", "evidence": "", "metrics": "", "warning_signs": ""}],
  "questions": [""],
  "sources": [{"title": "", "url": ""}]
}"""

FINANCIAL_SHAPE = """{
  "company": "full company name",
  "currency": "reporting currency and unit, e.g. 'USD millions'",
  "fiscal_year_end": "e.g. 'late June'",
  "as_of": "latest reporting period used",
  "periods": ["FY2024", "FY2025", "FY2026", "YTD FY2027 (Q1)", "YTD FY2026 (Q1)"],
  "metrics": [
    {"metric": "Revenue", "cells": [{"value": "", "source": 1}]},
    "... one row each, in this order: Revenue, Revenue growth (YoY), Operating margin, Operating cash flow, Capital expenditures, Free cash flow, Total debt, Cash & equivalents (incl. short-term investments), Diluted shares outstanding -- each with exactly one cell per period, same order as periods"
  ],
  "analysis": {
    "trends": "", "cash_flow_quality": "", "debt_risks": "",
    "dilution": "", "working_capital": ""
  },
  "peers": [{"company": "", "ticker": "", "period": "", "revenue_growth": "", "operating_margin": "",
             "fcf_margin": "", "leverage": "net debt / EBITDA or debt-to-equity, say which", "source": 1}],
  "red_flags": [{"flag": "", "severity": "High | Medium | Low", "evidence": ""}],
  "sources": [{"id": 1, "title": "", "url": ""}]
}"""


def _business_prompt(who, label):
    return (
        f"Analyze {who} over a {label} investment horizon. Break down its revenue streams, "
        "business segments, customer concentration, competitive advantages, and growth "
        "drivers. Identify the three biggest opportunities and three most material risks; "
        "for each give the supporting evidence, the key metrics to monitor, and the warning "
        "signs that the thesis is breaking. Conclude with the most important unanswered "
        "questions an investor should investigate.\n\n"
        "Search for the latest filings and earnings results first.\n\n"
        "Your final message must be ONLY a JSON object (no markdown fences, no other "
        f"text) in exactly this shape:\n{OUTPUT_SHAPE}\n"
        "Exactly 3 opportunities and 3 risks. 4-7 questions. List the URLs you actually "
        "relied on in sources."
    )


def _financial_prompt(who):
    return (
        f"Evaluate {who}'s financial strength using its latest three fiscal years and "
        "comparable year-to-date results (latest YTD vs the same YTD period a year "
        "earlier). Cover revenue, growth, operating margins, operating cash flow, capital "
        "expenditures, free cash flow, total debt, cash, and diluted shares outstanding. "
        "Explain major trends, cash-flow quality (e.g. OCF vs net income, reliance on "
        "working capital, stock-based compensation), debt risks (maturities, covenants, "
        "convertibles, interest coverage), potential dilution (convertibles, ATM programs, "
        "warrants, SBC), and working-capital changes. Benchmark against 2-4 relevant "
        "competitors on comparable periods, and flag any signs of financial deterioration.\n\n"
        "Get figures from the company's own filings (10-K/10-Q/20-F/annual and interim "
        "reports) or its investor-relations releases wherever possible.\n\n"
        "Citation rules: every cell's \"source\" is the id of the entry in sources that the "
        "figure came from. For a figure you calculated (growth, margins, FCF = OCF - capex), "
        "cite the filing its inputs came from and add ' (calc)' to the value. If a figure "
        "cannot be found, set value to 'n/a' and source to null -- never estimate it. If the "
        "company reports semi-annually or has no YTD period yet, still return 5 periods and "
        "label the last two to say what they are.\n\n"
        "Your final message must be ONLY a JSON object (no markdown fences, no other "
        f"text) in exactly this shape:\n{FINANCIAL_SHAPE}"
    )


REQUIRED = {
    "business": ("summary", "opportunities", "risks", "questions"),
    "financial": ("periods", "metrics", "analysis", "sources"),
}


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("equity_research: DATABASE_URL not set, skipping init")
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS equity_research_cache (
                    symbol     TEXT NOT NULL,
                    horizon    TEXT NOT NULL,
                    report     JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (symbol, horizon)
                )
            """)
        conn.commit()
    print("equity_research: table ready")


def _get_cached(symbol, horizon):
    if not DATABASE_URL:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT report, created_at FROM equity_research_cache "
                "WHERE symbol = %s AND horizon = %s AND created_at >= %s",
                (symbol, horizon, cutoff),
            )
            row = cur.fetchone()
    if not row:
        return None
    report, created_at = row
    return {"status": "ready", "report": report, "generated_at": created_at.isoformat()}


def _count_today():
    if not DATABASE_URL:
        return 0
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM equity_research_cache "
                "WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            )
            return cur.fetchone()[0]


def _save(symbol, horizon, report):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO equity_research_cache (symbol, horizon, report, created_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (symbol, horizon) DO UPDATE SET
                    report = EXCLUDED.report, created_at = EXCLUDED.created_at
            """, (symbol, horizon, Json(report)))
        conn.commit()


def _extract_json(response_content):
    """The final answer is the text after the last web search result. Web search
    citations split that text into several text blocks, so join them before
    parsing, then take the outermost {...} in case the model added a stray line."""
    last_tool = -1
    for i, block in enumerate(response_content):
        if block.type in ("server_tool_use", "web_search_tool_result"):
            last_tool = i
    text = "".join(b.text for b in response_content[last_tool + 1:] if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in final text")
    return json.loads(text[start:end + 1])


def _validate(report, kind):
    """Reject a report missing the sections the page renders, rather than
    caching a half-empty one for a week."""
    for key in REQUIRED[kind]:
        if not report.get(key):
            raise ValueError(f"report missing '{key}'")
    if kind == "business":
        report["opportunities"] = report["opportunities"][:3]
        report["risks"] = report["risks"][:3]
    else:
        # The model sometimes echoes the "... one row each" hint from the
        # shape as a literal string -- keep only real metric rows.
        report["metrics"] = [m for m in report["metrics"] if isinstance(m, dict) and m.get("cells")]
        if not report["metrics"]:
            raise ValueError("report has no metric rows")
    return report


def _generate(symbol, kind, horizon, company_name):
    who = f"{symbol} ({company_name})" if company_name else symbol
    today = f"Today is {datetime.now(timezone.utc).date().isoformat()}. "
    if kind == "financial":
        prompt = today + _financial_prompt(who)
        # Three annual reports + interim + peers needs more lookups.
        max_uses = 12
    else:
        prompt = today + _business_prompt(who, HORIZONS[horizon])
        max_uses = 8
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}]

    # A long server-tool turn can come back as pause_turn; resending the
    # partial assistant turn lets the server continue where it stopped.
    for _ in range(4):
        with _client.messages.stream(
            model="claude-opus-5-5",
            max_tokens=32000,
            output_config={"effort": "high"},
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise RuntimeError(f"refused (category={category})")
        if response.stop_reason == "pause_turn":
            messages = messages + [{"role": "assistant", "content": response.content}]
            continue
        if response.stop_reason == "max_tokens":
            raise RuntimeError("hit max_tokens before finishing")
        return _validate(_extract_json(response.content), kind)

    raise RuntimeError("still paused after 4 continuations")


def _run_job(symbol, kind, horizon, company_name):
    key = (symbol, cache_key(kind, horizon))
    try:
        report = _generate(symbol, kind, horizon, company_name)
        _save(symbol, key[1], report)
        with _jobs_lock:
            _jobs.pop(key, None)
    except Exception as e:
        print(f"equity_research: job failed for {symbol}/{key[1]}: {type(e).__name__}: {e}")
        with _jobs_lock:
            _jobs[key] = "failed"


def get_status(symbol, kind, horizon):
    """Polling read: never starts a job. Returns status ready | pending |
    failed | none (or unavailable when the feature isn't configured)."""
    if not _client or not DATABASE_URL:
        return {"status": "unavailable"}
    ck = cache_key(kind, horizon)
    cached = _get_cached(symbol, ck)
    if cached:
        return cached
    with _jobs_lock:
        state = _jobs.get((symbol, ck))
    return {"status": state or "none"}


def start(symbol, kind, horizon, company_name=None):
    """Returns the cached report if one is fresh, otherwise starts a background
    job (unless one is already running or today's cap is used up)."""
    if not _client or not DATABASE_URL:
        return {"status": "unavailable"}

    ck = cache_key(kind, horizon)
    cached = _get_cached(symbol, ck)
    if cached:
        return cached

    key = (symbol, ck)
    with _jobs_lock:
        if _jobs.get(key) == "pending":
            return {"status": "pending"}
        # Count in-flight jobs too, or several clicks in the same minute
        # could each pass the cap check before any of them has saved.
        in_flight = sum(1 for s in _jobs.values() if s == "pending")
        if _count_today() + in_flight >= DAILY_CAP:
            return {"status": "capped"}
        _jobs[key] = "pending"

    threading.Thread(target=_run_job, args=(symbol, kind, horizon, company_name), daemon=True).start()
    return {"status": "pending"}
