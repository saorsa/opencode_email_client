#!/usr/bin/env python3
"""
hook_poll.py - Lightweight plugin-based mode.

The opencode-email-bridge plugin (hook-notify-plugin.ts) triggers email
notifications on session completion. Cron runs this script to poll IMAP and
feed replies back via `opencode run -c`.

Setup:
  1. Copy hook-notify-plugin.ts to your project's .opencode/plugins/ directory
     to enable completion notifications.

  2. Add cron entry (every 2 minutes):
     */2 * * * * python3 /path/to/hook_poll.py poll >> /path/to/bridge.log 2>&1

Usage:
  python3 hook_poll.py poll          # One-shot IMAP poll, feed to last session
  python3 hook_poll.py notify-test   # Test email sending
"""

import email
import email.mime.text
import email.utils
import imaplib
import json
import logging
import os
import smtplib
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("hook-poll")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    p = Path(os.environ.get("OCBRIDGE_CONFIG", CONFIG_PATH))
    if not p.exists():
        log.error("Config not found: %s", p)
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def poll_imap(config: dict) -> list[dict]:
    """Poll IMAP for [opencode] emails."""
    cfg = config["imap"]
    results = []

    try:
        if cfg.get("use_ssl", True):
            conn = imaplib.IMAP4_SSL(cfg["host"], cfg.get("port", 993))
        else:
            conn = imaplib.IMAP4(cfg["host"], cfg.get("port", 143))

        conn.login(cfg["username"], cfg["password"])
        conn.select(cfg.get("folder", "INBOX"))

        prefix = cfg.get("subject_prefix", "[opencode]")
        criteria = f'(UNSEEN SUBJECT "{prefix}")'
        status, data = conn.search(None, criteria)

        if status != "OK":
            conn.logout()
            return []

        for num in data[0].split():
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            message_id = msg.get("Message-ID", "")
            subject = msg.get("Subject", "")
            from_addr = email.utils.parseaddr(msg.get("From", ""))[1]

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(errors="replace")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="replace")

            # Strip prefix
            clean_subject = subject
            if prefix and subject.lower().startswith(prefix.lower()):
                clean_subject = subject[len(prefix):].strip().lstrip(":")

            # Check for session ID in body
            session_id = None
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("session:"):
                    session_id = line.split(":", 1)[1].strip()
                    break

            results.append({
                "message_id": message_id,
                "from": from_addr,
                "subject": clean_subject,
                "body": body.strip(),
                "session_id": session_id,
            })

            conn.store(num, "+FLAGS", "\\Seen")

        conn.close()
        conn.logout()

    except Exception as e:
        log.error("IMAP poll failed: %s", e)

    return results


def feed_to_opencode(config: dict, email_data: dict):
    """Feed email content to opencode via CLI."""
    project_dir = config["opencode"].get("project_dir", ".")
    text = email_data.get("body") or email_data.get("subject", "")
    session_id = email_data.get("session_id")

    if session_id:
        cmd = ["opencode", "-s", session_id, "-c", "run", text]
    else:
        cmd = ["opencode", "-c", "run", text]

    log.info("Running: %s", " ".join(cmd[:6]) + "...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=project_dir,
        )
        log.info("opencode exited with code %d", result.returncode)
        if result.stdout:
            log.info("Output: %s", result.stdout[:500])
        if result.stderr:
            log.warning("Stderr: %s", result.stderr[:500])

        return result.returncode == 0

    except subprocess.TimeoutExpired:
        log.error("opencode timed out")
        return False
    except Exception as e:
        log.error("Failed to run opencode: %s", e)
        return False


def send_notification(config: dict, subject: str, body: str):
    """Send email notification."""
    smtp_cfg = config["smtp"]
    notify_cfg = config["notify"]

    msg = email.mime.text.MIMEText(body, "plain")
    msg["From"] = smtp_cfg.get("from_address", smtp_cfg["username"])
    msg["To"] = notify_cfg["recipient_email"]
    msg["Subject"] = subject

    try:
        port = smtp_cfg.get("port", 587)
        use_ssl = smtp_cfg.get("use_ssl", port == 465)
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_cfg["host"], port, timeout=30) as server:
                server.login(smtp_cfg["username"], smtp_cfg["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_cfg["host"], port, timeout=30) as server:
                if smtp_cfg.get("use_tls", True):
                    server.starttls()
                server.login(smtp_cfg["username"], smtp_cfg["password"])
                server.send_message(msg)
        log.info("Notification sent: %s", subject)
    except Exception as e:
        log.error("Failed to send notification: %s", e)


def poll_mode(config: dict):
    """Poll IMAP and feed to opencode."""
    emails = poll_imap(config)
    log.info("Found %d email(s)", len(emails))

    for em in emails:
        log.info("Processing: %s from %s", em["subject"], em["from"])
        success = feed_to_opencode(config, em)
        if success:
            send_notification(
                config,
                f"[opencode] Processed: {em['subject'][:80]}",
                f"Your instructions have been processed.\n\nOriginal subject: {em['subject']}\nFrom: {em['from']}",
            )


def notify_test_mode(config: dict):
    """Send a test notification."""
    send_notification(
        config,
        "[opencode] Bridge Test",
        f"opencode-email-bridge is working!\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    )
    print("Test notification sent")


def main():
    if len(sys.argv) < 2:
        print("Usage: hook_poll.py [poll|notify-test]")
        sys.exit(1)

    command = sys.argv[1]
    config = load_config()

    if command == "poll":
        poll_mode(config)
    elif command == "notify-test":
        notify_test_mode(config)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
