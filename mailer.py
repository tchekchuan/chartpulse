# ============================================================
# File: mailer.py
# Date: 2026-08-15
# Task: Shared Gmail SMTP sender for alerts.py and subscribers.py.
#
# Render's containers have no IPv6 route, but smtplib's default
# connection logic tries whatever getaddrinfo() returns first --
# smtp.gmail.com has an AAAA (IPv6) record, so it was failing with
# "[Errno 101] Network is unreachable" on every send in production
# (only ever verified locally, where IPv6 routing exists). Forcing
# an IPv4-only resolution before connecting fixes it.
# ============================================================

import os
import smtplib
import socket
from email.mime.text import MIMEText

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")


def _connect_ipv4(host, port, timeout=10):
    addr_info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    ipv4_addr = addr_info[0][4][0]
    server = smtplib.SMTP(timeout=timeout)
    server.connect(ipv4_addr, port)
    server._host = host  # starttls() validates the TLS cert against this, not the IP
    return server


def send_email(to_addr, subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        print("mailer: GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set, skipping email")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = to_addr
        server = _connect_ipv4("smtp.gmail.com", 587, timeout=10)
        try:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [to_addr], msg.as_string())
        finally:
            server.quit()
        return True
    except Exception as e:
        print(f"mailer: email to {to_addr} failed: {type(e).__name__}: {e}")
        return False
