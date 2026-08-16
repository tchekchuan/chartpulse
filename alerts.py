# ============================================================
# File: alerts.py
# Date: 2026-08-13 (subscriber My Portfolio/My Watchlist alerts added 2026-08-16)
# Task: Background job (runs inside the Render web process) that
#       twice daily checks the watchlist/portfolio and pushes a
#       Telegram alert for STRONG BUY ratings (any symbol) and for
#       BUY/SELL zone changes on held positions. Replaces the local
#       Task-Scheduler-based portfolio_alert.py for the cloud app,
#       since that job only fires when the PC is on.
#
#       Same cycle also personally alerts subscribers: any symbol in
#       their own My Portfolio (user_holdings.py) fires on a new
#       BUY/SELL zone change; any symbol in My Watchlist
#       (user_watchlist.py) fires only on a new STRONG BUY, mirroring
#       how Shawn's own PORTFOLIO/WATCHLIST lists already behave.
# ============================================================

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from mailer import send_email as _mailer_send_email
import track_record
import symbol_state
import user_holdings
import user_watchlist
import subscribers

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

ALERT_EMAIL_TO     = os.environ.get("ALERT_EMAIL_TO")

STATE_FILE = Path(__file__).parent / "alert_state.json"
SGT = timezone(timedelta(hours=8))

# Portfolio = actually held (alert on any BUY/SELL zone change).
# Watchlist = research only (alert only on STRONG BUY).
# Mirrors ivy/analyst/watchlist.json as of 2026-08-13 — update here if holdings change.
PORTFOLIO = ["BBAI", "DJT", "1810.HK", "MT", "CAST.ST", "WOLF", "BABA"]
WATCHLIST = ["JOBY", "ACHR", "NOW", "AAPL", "GOOGL", "META"]

CHECK_TIMES_SGT = [(8, 0), (21, 0)]   # 8am and 9pm SGT, matching the old local job


def _action_for(rating):
    if rating in ("STRONG BUY", "BUY"):
        return "BUY"
    if rating in ("SELL", "STRONG SELL"):
        return "SELL"
    return "HOLD"


def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("alerts: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping send")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"alerts: Telegram send failed: {e}")
        return False


def send_email(subject, body):
    if not ALERT_EMAIL_TO:
        print("alerts: ALERT_EMAIL_TO not set, skipping email")
        return False
    return _mailer_send_email(ALERT_EMAIL_TO, subject, body)


def _format_line(sym, r, tag, held_tag=""):
    return (
        f"*{sym}*{held_tag}: {tag} — {r['rating']}\n"
        f"  Price {r['price']:.2f} {r['currency']} ({r['change']:+.2f}%)  "
        f"RSI {r['rsi']:.0f}  Stage {r['stage']}\n"
        f"  {r['reason']}"
    )


def check_and_alert():
    """Runs one full check cycle. Safe to call manually for testing."""
    from app import analyze_symbol   # deferred: avoids import-order issues with app.py

    prev_state = _load_state()
    new_state  = {}
    lines            = []   # everything -- goes to Shawn only (Telegram + his email)
    strong_buy_lines = []   # STRONG BUY only -- also goes to public subscribers

    # Subscriber reverse indexes: {symbol: [email, ...]}. Fetched up front so
    # every symbol -- fixed watchlist AND anything subscribers added to their
    # own My Portfolio / My Watchlist -- gets rated exactly once this cycle,
    # even if several subscribers (or Shawn's own list) reference the same
    # symbol.
    try:
        portfolio_by_symbol = user_holdings.get_all_holdings_by_symbol()
    except Exception as e:
        print(f"alerts: user_holdings lookup failed: {e}")
        portfolio_by_symbol = {}
    try:
        watchlist_by_symbol = user_watchlist.get_all_watchlist_by_symbol()
    except Exception as e:
        print(f"alerts: user_watchlist lookup failed: {e}")
        watchlist_by_symbol = {}

    all_symbols = list(dict.fromkeys(
        PORTFOLIO + WATCHLIST + list(portfolio_by_symbol) + list(watchlist_by_symbol)
    ))

    rated = {}          # {symbol: r} -- shared across the fixed-list and subscriber logic below
    prior_symbol_state = symbol_state.get_all()
    new_symbol_state    = {}

    for sym in all_symbols:
        r = analyze_symbol(sym, period="1y")
        if "error" in r:
            new_state[sym] = prev_state.get(sym, {})
            continue
        rated[sym] = r

        rating = r["rating"]
        action = _action_for(rating)
        new_symbol_state[sym] = {"rating": rating, "action": action}

        # ── Fixed watchlist/portfolio (Shawn's own, unchanged logic) ──
        if sym in PORTFOLIO or sym in WATCHLIST:
            held = sym in PORTFOLIO
            prev = prev_state.get(sym, {})

            new_state[sym] = {
                "rating": rating, "action": action,
                "last_checked": datetime.now(SGT).isoformat(timespec="seconds"),
            }

            is_new_strong_buy = rating == "STRONG BUY" and prev.get("rating") != "STRONG BUY"
            is_held_change    = held and action in ("BUY", "SELL") and prev.get("action") != action

            if is_new_strong_buy or is_held_change:
                tag = "⭐ STRONG BUY" if rating == "STRONG BUY" else f"{action} zone"
                held_tag = " (held)" if held else ""
                line = _format_line(sym, r, tag, held_tag)
                lines.append(line)
                if is_new_strong_buy:
                    strong_buy_lines.append(line)

                # Backtest tracking: log the same moment a signal becomes
                # alert-worthy on the buy side (STRONG BUY anywhere, or a new
                # BUY zone on a held position) -- scoped to buy-side only for
                # now, sell-zone changes are exit timing, a separate question.
                if is_new_strong_buy or (is_held_change and action == "BUY"):
                    try:
                        track_record.log_signal(
                            sym, rating, r.get("score"),
                            r.get("entry"), r.get("stop"), r.get("target"),
                        )
                    except Exception as e:
                        print(f"alerts: track_record.log_signal failed for {sym}: {e}")

    _save_state(new_state)

    # ── Subscriber My Portfolio / My Watchlist alerts ─────────────────────
    # Portfolio symbols: alert on any new BUY/SELL zone change (you hold it).
    # Watchlist symbols: alert only on a new STRONG BUY (you don't hold it,
    # so a SELL signal isn't actionable the same way).
    per_subscriber_lines = {}   # {email: [line, ...]}

    def _add_line(email, line):
        per_subscriber_lines.setdefault(email, []).append(line)

    for sym, r in rated.items():
        prior  = prior_symbol_state.get(sym, {})
        rating = r["rating"]
        action = _action_for(rating)

        if sym in portfolio_by_symbol and action in ("BUY", "SELL") and prior.get("action") != action:
            line = _format_line(sym, r, f"{action} zone (your portfolio)")
            for email in portfolio_by_symbol[sym]:
                _add_line(email, line)

        if sym in watchlist_by_symbol and rating == "STRONG BUY" and prior.get("rating") != "STRONG BUY":
            line = _format_line(sym, r, "⭐ STRONG BUY (your watchlist)")
            for email in watchlist_by_symbol[sym]:
                _add_line(email, line)

    try:
        symbol_state.set_many(new_symbol_state)
    except Exception as e:
        print(f"alerts: symbol_state.set_many failed: {e}")

    if per_subscriber_lines:
        try:
            token_by_email = {e: t for e, t in subscribers.get_confirmed_subscribers()}
            sent = 0
            for email, sub_lines in per_subscriber_lines.items():
                token = token_by_email.get(email)
                if not token:
                    continue  # not (or no longer) a confirmed subscriber
                unsub_url = f"{subscribers.SITE_URL}/api/subscribe/unsubscribe?token={token}"
                plain = "getChartPulse: your portfolio/watchlist update\n\n" + "\n\n".join(
                    l.replace("*", "") for l in sub_lines
                ) + f"\n\n---\nManage your lists: {subscribers.SITE_URL} (log in with this email)\nUnsubscribe: {unsub_url}"
                if _mailer_send_email(email, f"getChartPulse: {len(sub_lines)} update(s) on your lists", plain):
                    sent += 1
            print(f"alerts: sent personalized portfolio/watchlist alerts to {sent} subscriber(s)")
        except Exception as e:
            print(f"alerts: subscriber portfolio/watchlist notify failed: {e}")

    if lines:
        now = datetime.now(SGT).strftime("%Y-%m-%d %H:%M SGT")
        msg = f"📈 *getChartPulse Alert*\n{now}\n\n" + "\n\n".join(lines)
        send_telegram(msg)
        plain = f"getChartPulse Alert — {now}\n\n" + "\n\n".join(
            l.replace("*", "") for l in lines
        )
        send_email(f"getChartPulse Alert — {len(lines)} update(s)", plain)
        print(f"alerts: sent {len(lines)} alert(s)")
    else:
        print("alerts: no actionable changes this cycle")

    if strong_buy_lines:
        try:
            now = datetime.now(SGT).strftime("%Y-%m-%d %H:%M SGT")
            public_plain = f"getChartPulse — {now}\n\n" + "\n\n".join(
                l.replace("*", "") for l in strong_buy_lines
            )
            sent = subscribers.send_strong_buy_alert(public_plain)
            print(f"alerts: sent STRONG BUY email to {sent} subscriber(s)")
        except Exception as e:
            print(f"alerts: subscriber notify failed: {e}")

    try:
        n = track_record.resolve_pending()
        if n:
            print(f"alerts: resolved {n} pending track_record signal(s)")
    except Exception as e:
        print(f"alerts: track_record.resolve_pending failed: {e}")


def _seconds_until_next_check():
    now = datetime.now(SGT)
    candidates = []
    for h, m in CHECK_TIMES_SGT:
        for day_offset in (0, 1):
            t = (now + timedelta(days=day_offset)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                candidates.append(t)
    next_run = min(candidates)
    return (next_run - now).total_seconds(), next_run


def _scheduler_loop():
    while True:
        wait_s, next_run = _seconds_until_next_check()
        print(f"alerts: next check at {next_run.isoformat()} (sleeping {wait_s/3600:.1f}h)")
        time.sleep(wait_s)
        try:
            check_and_alert()
        except Exception as e:
            print(f"alerts: check_and_alert failed: {e}")


_scheduler_started = False


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print("alerts: scheduler thread started")
