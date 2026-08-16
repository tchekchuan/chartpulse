# ============================================================
# File: alerts.py
# Date: 2026-08-13
# Task: Background job (runs inside the Render web process) that
#       twice daily checks the watchlist/portfolio and pushes a
#       Telegram alert for STRONG BUY ratings (any symbol) and for
#       BUY/SELL zone changes on held positions. Replaces the local
#       Task-Scheduler-based portfolio_alert.py for the cloud app,
#       since that job only fires when the PC is on.
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


def check_and_alert():
    """Runs one full check cycle. Safe to call manually for testing."""
    from app import analyze_symbol   # deferred: avoids import-order issues with app.py

    prev_state = _load_state()
    new_state  = {}
    lines            = []   # everything -- goes to Shawn only (Telegram + his email)
    strong_buy_lines = []   # STRONG BUY only -- also goes to public subscribers

    all_symbols = list(dict.fromkeys(PORTFOLIO + WATCHLIST))  # dedup, keep order
    for sym in all_symbols:
        r = analyze_symbol(sym, period="1y")
        if "error" in r:
            new_state[sym] = prev_state.get(sym, {})
            continue

        rating = r["rating"]
        action = _action_for(rating)
        held   = sym in PORTFOLIO
        prev   = prev_state.get(sym, {})

        new_state[sym] = {
            "rating": rating, "action": action,
            "last_checked": datetime.now(SGT).isoformat(timespec="seconds"),
        }

        is_new_strong_buy = rating == "STRONG BUY" and prev.get("rating") != "STRONG BUY"
        is_held_change    = held and action in ("BUY", "SELL") and prev.get("action") != action

        if is_new_strong_buy or is_held_change:
            tag = "⭐ STRONG BUY" if rating == "STRONG BUY" else f"{action} zone"
            held_tag = " (held)" if held else ""
            line = (
                f"*{sym}*{held_tag}: {tag} — {rating}\n"
                f"  Price {r['price']:.2f} {r['currency']} ({r['change']:+.2f}%)  "
                f"RSI {r['rsi']:.0f}  Stage {r['stage']}\n"
                f"  {r['reason']}"
            )
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
            import subscribers
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
