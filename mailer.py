# ============================================================
# File: mailer.py
# Date: 2026-08-15
# Task: Shared email sender for alerts.py and subscribers.py.
#
# Originally used Gmail SMTP. Render blocks outbound SMTP ports
# entirely (confirmed via production logs: IPv6 "Network is
# unreachable", then even after forcing IPv4, a silent connection
# TimeoutError -- consistent with a firewall DROP rule, common on
# free-tier hosting to prevent spam abuse). Switched to Resend's
# HTTP API instead -- port 443 is never blocked, since that would
# break the platform's own core purpose of serving web traffic.
#
# 2026-08-16: getchartpulse.com verified as a sending domain in
# Resend (DKIM/SPF/MX records added to GoDaddy) -- switched off
# Resend's shared onboarding@resend.dev test sender, which could
# only reliably deliver to the account owner's own address. Real
# external subscribers can now actually receive mail.
# ============================================================

import os
import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
FROM_ADDRESS    = "getChartPulse <alerts@getchartpulse.com>"


def send_email(to_addr, subject, body):
    if not RESEND_API_KEY:
        print("mailer: RESEND_API_KEY not set, skipping email")
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": FROM_ADDRESS,
                "to": [to_addr],
                "subject": subject,
                "text": body,
            },
            timeout=10,
        )
        if r.status_code >= 300:
            print(f"mailer: email to {to_addr} failed: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"mailer: email to {to_addr} failed: {type(e).__name__}: {e}")
        return False
