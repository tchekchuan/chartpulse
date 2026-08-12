# ============================================================
# File: portfolio_alert.py
# Date: 2026-07-12
# Author: IVY - Personal Stock Analyst
# Task: Scheduled portfolio watcher. Screens only the tickers
#       actually HELD (ivy/analyst/watchlist.json -> "portfolio"),
#       compares each rating against the last known state, and
#       sends a Telegram + Windows toast notification only when
#       a holding crosses into (or out of) an actionable BUY/SELL
#       zone. Designed to run standalone via Windows Task
#       Scheduler twice daily - does NOT require app.py / the
#       Flask dashboard to be running.
# Usage: python portfolio_alert.py
# ============================================================

import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

STOCK_MONITOR_DIR = Path(r"C:\Users\65910\projects\stock-monitor")
sys.path.insert(0, str(STOCK_MONITOR_DIR))
from app import analyze_symbol  # noqa: E402  (needs sys.path set first)

WATCHLIST_FILE  = Path(r"C:\Users\65910\OneDrive\Virtual_IT_Dept\ivy\analyst\watchlist.json")
STATE_FILE      = Path(r"C:\Users\65910\OneDrive\Virtual_IT_Dept\ivy\analyst\portfolio_alert_state.json")
NOTIFY_SCRIPT   = Path(r"C:\Users\65910\OneDrive\Virtual_IT_Dept\virtual-it\tools\send_notification.py")
PYTHON_EXE      = r"C:\Users\65910\anaconda3\python.exe"


def load_portfolio_symbols():
    if not WATCHLIST_FILE.exists():
        return []
    data  = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    items = data.get("portfolio", [])

    symbols, seen = [], set()
    for item in items:
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        market = item.get("market", "")
        if market == "HKEX" and not ticker.endswith(".HK"):
            ticker = f"{ticker}.HK"
        elif "Stockholm" in market and not ticker.endswith(".ST"):
            ticker = f"{ticker}.ST"
        if ticker not in seen:
            seen.add(ticker)
            symbols.append(ticker)
    return symbols


def action_for(rating):
    if rating in ("STRONG BUY", "BUY"):
        return "BUY"
    if rating in ("SELL", "STRONG SELL"):
        return "SELL"
    if rating == "MILD BUY":
        return "WATCH"
    return "HOLD"


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def fmt_price(v, currency):
    if v is None:
        return "n/a"
    return f"{v:,.2f} {currency}"


def send_notification(title, message):
    try:
        subprocess.run(
            [PYTHON_EXE, str(NOTIFY_SCRIPT), title, message],
            timeout=30, check=False,
        )
    except Exception as e:
        print(f"  Notification send failed: {e}")


def main():
    symbols = load_portfolio_symbols()
    if not symbols:
        print("  No holdings in portfolio[] — nothing to check.")
        return

    print(f"  Checking {len(symbols)} holdings: {', '.join(symbols)}")

    prev_state = load_state()
    new_state  = {}
    changes    = []

    for sym in symbols:
        r = analyze_symbol(sym, period="1y", interval="1d")
        if "error" in r:
            print(f"  {sym}: SKIP ({r['error']})")
            new_state[sym] = prev_state.get(sym, {})
            continue

        action = action_for(r["rating"])
        prev_action = (prev_state.get(sym) or {}).get("action")

        new_state[sym] = {
            "action":       action,
            "rating":       r["rating"],
            "score":        r["score"],
            "last_checked": datetime.now().isoformat(timespec="seconds"),
        }

        is_actionable = action in ("BUY", "SELL")
        changed        = prev_action != action

        if is_actionable and (changed or prev_action is None):
            line = (
                f"{sym}: now {action} ({r['rating']}, score {r['score']:+d})\n"
                f"  Price {fmt_price(r['price'], r['currency'])} ({r['change']:+.2f}%)  "
                f"Stage: {r['stage_label']}  RSI {r['rsi']:.1f}\n"
                f"  {r['reason']}"
            )
            if r.get("entry") is not None:
                line += (f"\n  Entry {fmt_price(r['entry'], r['currency'])}  "
                          f"Stop {fmt_price(r.get('stop'), r['currency'])}  "
                          f"Target {fmt_price(r.get('target'), r['currency'])}")
            changes.append(line)
            print(f"  {sym}: {prev_action or '(new)'} -> {action}  [ALERT]")
        else:
            print(f"  {sym}: {action} (unchanged)")

    save_state(new_state)

    if changes:
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        title = "ChartPulse Portfolio Alert"
        message = f"{today}\n\n" + "\n\n".join(changes)
        send_notification(title, message)
        print(f"\n  Sent alert for {len(changes)} holding(s) with an actionable change.")
    else:
        print("\n  No actionable changes since last check — no notification sent.")


if __name__ == "__main__":
    main()
