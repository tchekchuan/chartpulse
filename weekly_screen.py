# ============================================================
# File: weekly_screen.py
# Date: 2026-07-12
# Author: IVY - Personal Stock Analyst
# Task: Screens the full watchlist (watchlist + portfolio) by
#       calling analyze_symbol() directly, ranks setups by
#       analyst score, and prints a buy/hold/sell verdict for
#       each ticker plus the single strongest setup of the
#       week. Saves a dated report copy and pushes a condensed
#       summary to Telegram. Runs standalone - does NOT require
#       app.py / the Flask dashboard to be running (same
#       pattern as portfolio_alert.py), so it works unattended
#       via Windows Task Scheduler.
# Usage: python weekly_screen.py
# ============================================================

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

STOCK_MONITOR_DIR = Path(r"C:\Users\65910\projects\stock-monitor")
sys.path.insert(0, str(STOCK_MONITOR_DIR))
from app import analyze_symbol  # noqa: E402  (needs sys.path set first)

WATCHLIST_FILE = Path(r"C:\Users\65910\OneDrive\Virtual_IT_Dept\ivy\analyst\watchlist.json")
REPORTS_DIR    = Path(r"C:\Users\65910\OneDrive\Virtual_IT_Dept\work\regional\reports")
NOTIFY_SCRIPT  = Path(r"C:\Users\65910\OneDrive\Virtual_IT_Dept\virtual-it\tools\send_notification.py")
PYTHON_EXE     = r"C:\Users\65910\anaconda3\python.exe"


class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; RED = "\033[91m"; GREEN = "\033[92m"
    YELLOW = "\033[93m"; BLUE = "\033[94m"; MAGENTA = "\033[95m"; CYAN = "\033[96m"; DIM = "\033[2m"


def c(color, text):
    return f"{color}{text}{C.RESET}"


ACTION_COLOR = {
    "BUY": C.GREEN + C.BOLD,
    "WATCH / SMALL BUY": C.YELLOW,
    "HOLD": C.BLUE,
    "SELL": C.RED + C.BOLD,
}


def load_watchlist_symbols():
    if not WATCHLIST_FILE.exists():
        print(c(C.RED, f"  Watchlist not found: {WATCHLIST_FILE}"))
        return []

    data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    raw_items = list(data.get("watchlist", [])) + list(data.get("portfolio", []))

    symbols, seen = [], set()
    for item in raw_items:
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


def fetch_screen(symbols, period="1y", interval="1d"):
    """Analyse each symbol directly (no HTTP dependency on app.py).
    max_workers=3 - higher concurrency triggers Yahoo Finance rate limiting."""
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(analyze_symbol, s, period, interval): s for s in symbols}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def action_for(rating):
    if rating in ("STRONG BUY", "BUY"):
        return "BUY"
    if rating in ("SELL", "STRONG SELL"):
        return "SELL"
    if rating == "MILD BUY":
        return "WATCH / SMALL BUY"
    return "HOLD"


def fmt_price(v, currency):
    if v is None:
        return "n/a"
    return f"{v:,.2f} {currency}"


def build_report(results):
    ok     = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    ok.sort(key=lambda r: r.get("score", -99), reverse=True)

    lines = []
    today = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"CONFLUENCE — WEEKLY MARKET SCREEN - {today}")
    lines.append("=" * 72)

    if not ok:
        lines.append("No tickers returned usable data.")
        for f in failed:
            lines.append(f"  {f['symbol']}: {f.get('error','unknown error')}")
        return "\n".join(lines), ok, failed

    best = ok[0]
    lines.append("")
    lines.append(f"STRONGEST SETUP OF THE WEEK: {best['symbol']}  ({best['rating']}, score {best['score']:+d})")
    lines.append(f"  Price   : {fmt_price(best['price'], best['currency'])}  ({best['change']:+.2f}% )")
    lines.append(f"  Stage   : {best['stage_label']}   RSI: {best['rsi']:.1f} ({best['rsi_zone']})")
    if best.get("entry") is not None:
        lines.append(f"  Entry   : {fmt_price(best['entry'], best['currency'])}  ({best.get('stars',0)}* confluence)")
    lines.append(f"  Stop    : {fmt_price(best.get('stop'), best['currency'])}")
    lines.append(f"  Target  : {fmt_price(best.get('target'), best['currency'])}")
    lines.append(f"  R:R     : {best.get('rr', 0):.2f}:1")
    lines.append(f"  Why     : {best['reason']}")
    lines.append(f"  News    : {best['sentiment']}")
    lines.append("")
    lines.append("-" * 72)
    lines.append(f"{'#':<3}{'Symbol':<10}{'Action':<20}{'Rating':<14}{'Stage':<10}{'RSI':<7}{'Score':<7}")
    lines.append("-" * 72)
    for i, r in enumerate(ok, 1):
        action = action_for(r["rating"])
        lines.append(
            f"{i:<3}{r['symbol']:<10}{action:<20}{r['rating']:<14}"
            f"S{r['stage']:<9}{r['rsi']:<7.1f}{r['score']:<+7d}"
        )
    lines.append("-" * 72)
    lines.append("")
    lines.append("DETAIL")
    lines.append("-" * 72)
    for r in ok:
        action = action_for(r["rating"])
        lines.append(f"{r['symbol']} - {action} ({r['rating']}, score {r['score']:+d})")
        lines.append(f"  Price {fmt_price(r['price'], r['currency'])} ({r['change']:+.2f}%)  "
                      f"Stage: {r['stage_label']}  RSI {r['rsi']:.1f} ({r['rsi_zone']})")
        if r.get("entry") is not None:
            lines.append(f"  Entry {fmt_price(r['entry'], r['currency'])}  "
                          f"Stop {fmt_price(r.get('stop'), r['currency'])}  "
                          f"Target {fmt_price(r.get('target'), r['currency'])}  "
                          f"R:R {r.get('rr',0):.2f}:1  ({r.get('stars',0)}* confluence)")
        lines.append(f"  Reason: {r['reason']}")
        lines.append(f"  News sentiment: {r['sentiment']}")
        lines.append("")

    if failed:
        lines.append("SKIPPED (no data)")
        lines.append("-" * 72)
        for f in failed:
            lines.append(f"  {f['symbol']}: {f.get('error','unknown error')}")

    return "\n".join(lines), ok, failed


def print_colored(ok, failed, best):
    today = datetime.now().strftime("%Y-%m-%d")
    print()
    print(c(C.CYAN + C.BOLD, f"  CONFLUENCE — WEEKLY MARKET SCREEN - {today}"))
    print(c(C.DIM, "  " + "=" * 68))

    if not ok:
        print(c(C.RED, "  No tickers returned usable data."))
        return

    print()
    print(c(C.MAGENTA + C.BOLD, f"  STRONGEST SETUP OF THE WEEK: {best['symbol']}") +
          c(C.GREEN if best["score"] >= 0 else C.RED, f"  [{best['rating']}, score {best['score']:+d}]"))
    print(f"    Price : {fmt_price(best['price'], best['currency'])}  ({best['change']:+.2f}%)")
    print(f"    Stage : {best['stage_label']}   RSI: {best['rsi']:.1f} ({best['rsi_zone']})")
    if best.get("entry") is not None:
        print(f"    Entry : {fmt_price(best['entry'], best['currency'])}  ({best.get('stars',0)}* confluence)")
    print(f"    Stop  : {fmt_price(best.get('stop'), best['currency'])}   "
          f"Target: {fmt_price(best.get('target'), best['currency'])}   "
          f"R:R {best.get('rr', 0):.2f}:1")
    print(f"    Why   : {best['reason']}")
    print()
    print(c(C.DIM, "  " + "-" * 68))
    print(f"  {'#':<3}{'Symbol':<10}{'Action':<20}{'Rating':<14}{'Stage':<8}{'RSI':<7}{'Score':<6}")
    print(c(C.DIM, "  " + "-" * 68))
    for i, r in enumerate(ok, 1):
        action = action_for(r["rating"])
        color  = ACTION_COLOR.get(action, C.RESET)
        print(f"  {i:<3}{r['symbol']:<10}{c(color, f'{action:<20}')}{r['rating']:<14}"
              f"S{r['stage']:<7}{r['rsi']:<7.1f}{r['score']:<+6d}")
    print(c(C.DIM, "  " + "-" * 68))

    if failed:
        print()
        print(c(C.YELLOW, "  Skipped (no data):"))
        for f in failed:
            print(c(C.DIM, f"    {f['symbol']}: {f.get('error','unknown error')}"))
    print()


def save_report(text):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path  = REPORTS_DIR / f"{today}_stock-screen_weekly-market-scan.md"
    if path.exists():
        v = 2
        while (REPORTS_DIR / f"{today}_stock-screen_weekly-market-scan_v{v}.md").exists():
            v += 1
        path = REPORTS_DIR / f"{today}_stock-screen_weekly-market-scan_v{v}.md"
    path.write_text("```\n" + text + "\n```\n", encoding="utf-8")
    return path


def build_telegram_message(ok, failed, best):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"{today}", ""]
    lines.append(f"STRONGEST SETUP: {best['symbol']} ({best['rating']}, score {best['score']:+d})")
    lines.append(f"Price {fmt_price(best['price'], best['currency'])} ({best['change']:+.2f}%)  "
                 f"Stage {best['stage_label']}  RSI {best['rsi']:.1f}")
    if best.get("entry") is not None:
        lines.append(f"Entry {fmt_price(best['entry'], best['currency'])}  "
                     f"Stop {fmt_price(best.get('stop'), best['currency'])}  "
                     f"Target {fmt_price(best.get('target'), best['currency'])}  "
                     f"R:R {best.get('rr', 0):.2f}:1")
    lines.append(f"Why: {best['reason']}")
    lines.append("")
    lines.append("ALL TICKERS")
    for r in ok:
        action = action_for(r["rating"])
        lines.append(f"{r['symbol']}: {action} ({r['rating']}, score {r['score']:+d})  "
                     f"{fmt_price(r['price'], r['currency'])} ({r['change']:+.2f}%)")
    if failed:
        lines.append("")
        lines.append("SKIPPED (no data): " + ", ".join(f["symbol"] for f in failed))
    return "\n".join(lines)


def push_telegram(message):
    try:
        subprocess.run(
            [PYTHON_EXE, str(NOTIFY_SCRIPT), "Weekly Market Screen", message, "--telegram-only"],
            timeout=30, check=False,
        )
    except Exception as e:
        print(c(C.RED, f"  Telegram push failed: {e}"))


def main():
    symbols = load_watchlist_symbols()
    if not symbols:
        print(c(C.RED, "  Watchlist is empty — add tickers to watchlist.json first."))
        sys.exit(1)

    print(c(C.CYAN, f"  Screening {len(symbols)} tickers: {', '.join(symbols)}"))
    results = fetch_screen(symbols)

    text, ok, failed = build_report(results)
    if ok:
        print_colored(ok, failed, ok[0])
        report_path = save_report(text)
        print(c(C.DIM, f"  Full report saved to: {report_path}"))
        push_telegram(build_telegram_message(ok, failed, ok[0]))
    else:
        print(text)


if __name__ == "__main__":
    main()
