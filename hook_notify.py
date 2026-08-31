#!/usr/bin/env python3
"""
hook_notify.py - Called by opencode's experimental hook system.

This script is invoked by opencode when a session completes.
It sends an email notification with the session summary.

Hook config in opencode.json:
  "experimental": {
    "hook": {
      "session_completed": [
        {"command": ["python3", "/path/to/hook_notify.py", "{session_id}", "{session_title}"]}
      ]
    }
  }
"""

import email.mime.text
import json
import logging
import os
import smtplib
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("hook-notify")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    p = Path(os.environ.get("OCBRIDGE_CONFIG", CONFIG_PATH))
    if not p.exists():
        log.error("Config not found: %s", p)
        return {}
    with open(p) as f:
        return json.load(f)


def send_email(config: dict, to: str, subject: str, body: str) -> bool:
    smtp_cfg = config.get("smtp", {})
    if not smtp_cfg:
        log.error("SMTP config missing")
        return False

    msg = email.mime.text.MIMEText(body, "plain")
    msg["From"] = smtp_cfg.get("from_address", smtp_cfg.get("username", ""))
    msg["To"] = to
    msg["Subject"] = subject

    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg.get("port", 587), timeout=30) as server:
            if smtp_cfg.get("use_tls", True):
                server.starttls()
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            server.send_message(msg)
        log.info("Email sent: %s", subject)
        return True
    except Exception as e:
        log.error("Email failed: %s", e)
        return False


def main():
    config = load_config()
    if not config:
        sys.exit(1)

    notify_cfg = config.get("notify", {})
    if not notify_cfg.get("on_task_complete", True):
        return

    session_id = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    session_title = sys.argv[2] if len(sys.argv) > 2 else "Unknown Task"

    recipient = notify_cfg.get("recipient_email")
    if not recipient:
        log.error("No recipient email configured")
        sys.exit(1)

    subject = f"[opencode] Task Complete: {session_title}"
    body = (
        f"Session completed!\n\n"
        f"Title: {session_title}\n"
        f"ID: {session_id}\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"To continue, reply with:\n"
        f"Subject: [opencode] <your instructions>\n"
        f"Body: session: {session_id}\n\n"
        f"Then describe what you want next."
    )

    send_email(config, recipient, subject, body)


if __name__ == "__main__":
    main()
