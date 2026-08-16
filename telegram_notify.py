# ============================================================
# File: telegram_notify.py
# Date: 2026-08-16
# Task: Shared Telegram send helper, extracted from alerts.py so other
#       modules (e.g. subscribers.py, for new-subscriber pings) can
#       notify Shawn without importing alerts.py -- alerts.py already
#       imports subscribers.py, so the reverse import would circular.
# ============================================================

import os

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("telegram_notify: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping send")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            # Telegram rejects the whole message on things like unbalanced
            # Markdown entities (e.g. a stray "_" or "*") -- that used to
            # fail completely silently here, since only network-level
            # exceptions were logged below. Now the actual API error is
            # visible in Render logs instead of just "nothing arrived".
            print(f"telegram_notify: Telegram API returned {r.status_code}: {r.text[:300]}")
        return r.status_code == 200
    except Exception as e:
        print(f"telegram_notify: Telegram send failed: {e}")
        return False
