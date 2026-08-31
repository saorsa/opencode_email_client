#!/usr/bin/env python3
"""
opencode-email-bridge

Email ↔ OpenCode bridge. Monitors opencode server events via SSE,
sends email notifications on task completion/errors, polls IMAP for
user replies, and feeds them back into opencode sessions.

Modes:
  serve  - Run the bridge daemon (default)
  poll   - One-shot IMAP poll + feed to opencode
  notify - Send a test notification
  status - Show running sessions

Usage:
  python3 bridge.py serve
  python3 bridge.py poll
  python3 bridge.py notify "test message"
  python3 bridge.py status
"""

import argparse
import email
import email.mime.text
import email.utils
import hashlib
import imaplib
import json
import logging
import os
import re
import queue
import signal
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_DB_PATH = Path(__file__).parent / "bridge_state.db"
LOG_PATH = Path(__file__).parent / "bridge.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(LOG_PATH),
    ],
)
log = logging.getLogger("opencode-bridge")


def load_config(path: Path | None = None) -> dict:
    p = path or Path(os.environ.get("OCBRIDGE_CONFIG", DEFAULT_CONFIG_PATH))
    if not p.exists():
        log.error("Config not found: %s", p)
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# State DB - tracks processed emails, session mappings
# ---------------------------------------------------------------------------


class StateDB:
    def __init__(self, path: Path = STATE_DB_PATH):
        self.conn = sqlite3.connect(str(path))
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id TEXT PRIMARY KEY,
                subject TEXT,
                session_id TEXT,
                processed_at REAL,
                direction TEXT
            );
            CREATE TABLE IF NOT EXISTS session_map (
                session_id TEXT PRIMARY KEY,
                project_dir TEXT,
                title TEXT,
                last_activity REAL,
                status TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                command TEXT,
                created_at REAL,
                executed INTEGER DEFAULT 0
            );
            """
        )
        self.conn.commit()

    def is_email_processed(self, message_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)
        )
        return cur.fetchone() is not None

    def mark_email_processed(
        self, message_id: str, subject: str, session_id: str, direction: str
    ):
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_emails (message_id, subject, session_id, processed_at, direction) VALUES (?, ?, ?, ?, ?)",
            (message_id, subject, session_id, time.time(), direction),
        )
        self.conn.commit()

    def upsert_session(
        self, session_id: str, project_dir: str, title: str, status: str
    ):
        self.conn.execute(
            "INSERT INTO session_map (session_id, project_dir, title, last_activity, status) VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET last_activity=?, status=?",
            (session_id, project_dir, title, time.time(), status, time.time(), status),
        )
        self.conn.commit()

    def get_active_sessions(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT session_id, project_dir, title, last_activity, status FROM session_map WHERE status != 'deleted' ORDER BY last_activity DESC"
        )
        return [
            dict(zip(["session_id", "project_dir", "title", "last_activity", "status"], row))
            for row in cur.fetchall()
        ]

    def add_pending_command(self, session_id: str, command: str):
        self.conn.execute(
            "INSERT INTO pending_commands (session_id, command, created_at) VALUES (?, ?, ?)",
            (session_id, command, time.time()),
        )
        self.conn.commit()

    def get_pending_commands(self, session_id: str) -> list[str]:
        cur = self.conn.execute(
            "SELECT id, command FROM pending_commands WHERE session_id = ? AND executed = 0 ORDER BY created_at",
            (session_id,),
        )
        rows = cur.fetchall()
        return [(r[0], r[1]) for r in rows]

    def mark_command_executed(self, cmd_id: int):
        self.conn.execute("UPDATE pending_commands SET executed = 1 WHERE id = ?", (cmd_id,))
        self.conn.commit()


# ---------------------------------------------------------------------------
# OpenCode HTTP Client
# ---------------------------------------------------------------------------


class OpenCodeClient:
    # Sentinel returned for a successful (2xx) response with an empty body.
    # Distinguishes "ok, no content" (204) from an actual failure (None).
    EMPTY_OK = object()

    def __init__(self, server_url: str):
        self.base = server_url.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None) -> dict | str | None:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                if not raw.strip():
                    # 2xx with empty body (e.g. 204 No Content) is a success.
                    return self.EMPTY_OK
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            log.error("HTTP %s %s -> %d: %s", method, path, e.code, body_text[:200])
            return None
        except Exception as e:
            log.error("Request failed: %s %s -> %s", method, path, e)
            return None

    def list_sessions(self) -> list[dict]:
        result = self._req("GET", "/session")
        return result if isinstance(result, list) else []

    def get_session(self, session_id: str) -> dict | None:
        result = self._req("GET", f"/session/{session_id}")
        return result if isinstance(result, dict) else None

    def create_session(self, title: str | None = None, project_dir: str | None = None) -> str | None:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if project_dir:
            body["directory"] = project_dir
        result = self._req("POST", "/session", body)
        if isinstance(result, dict) and "id" in result:
            return result["id"]
        return None

    def send_message(self, session_id: str, text: str) -> dict | None:
        return self._req("POST", f"/session/{session_id}/message", {"parts": [{"type": "text", "text": text}]})

    def send_message_async(self, session_id: str, text: str) -> dict | None:
        result = self._req("POST", f"/session/{session_id}/prompt_async", {"parts": [{"type": "text", "text": text}]})
        # prompt_async returns 204 No Content on success -> EMPTY_OK sentinel.
        return None if result is None else (result if result is not self.EMPTY_OK else {})

    def send_command(self, session_id: str, command: str) -> dict | None:
        return self._req("POST", f"/session/{session_id}/command", {"command": command})

    def get_last_output(self, session_id: str) -> str:
        """Fetch the last assistant (task-complete) text for a session."""
        try:
            result = self._req("GET", f"/session/{session_id}/message?limit=10")
            if not isinstance(result, list):
                return ""
            for entry in reversed(result):
                info = entry.get("info", {}) if isinstance(entry, dict) else {}
                if info.get("role") != "assistant":
                    continue
                parts = entry.get("parts", [])
                text = "\n".join(
                    p.get("text", "")
                    for p in parts
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
                if text:
                    return text
        except Exception as e:
            log.error("Failed to fetch last output for %s: %s", session_id, e)
        return ""

    def get_events(self, session_id: str | None = None) -> urllib.request.Request:
        path = f"/session/{session_id}/event" if session_id else "/global/event"
        url = f"{self.base}{path}"
        return urllib.request.Request(url)


# ---------------------------------------------------------------------------
# Email Monitor (IMAP)
# ---------------------------------------------------------------------------


class EmailMonitor:
    def __init__(self, config: dict, state: StateDB):
        self.cfg = config["imap"]
        self.state = state
        self.conn: imaplib.IMAP4_SSL | None = None
        self._connect()

    def _connect(self):
        host = self.cfg["host"]
        port = self.cfg.get("port", 993)
        log.info("Connecting to IMAP %s:%s", host, port)
        if self.cfg.get("use_ssl", True):
            self.conn = imaplib.IMAP4_SSL(host, port)
        else:
            self.conn = imaplib.IMAP4(host, port)
        self.conn.login(self.cfg["username"], self.cfg["password"])
        self.conn.select(self.cfg.get("folder", "INBOX"))
        log.info("IMAP connected")

    def _ensure_connected(self):
        try:
            self.conn.noop()
        except Exception:
            log.warning("IMAP connection lost, reconnecting")
            self._connect()

    def poll(self) -> list[dict]:
        """Poll for unread emails matching subject prefix. Returns list of {message_id, from, subject, body, date}."""
        self._ensure_connected()
        criteria = "UNSEEN" if self.cfg.get("unread_only", True) else "ALL"
        prefix = self.cfg.get("subject_prefix", "[opencode]")

        if prefix:
            criteria = f'({criteria} SUBJECT "{prefix}")'

        status, data = self.conn.search(None, criteria)
        if status != "OK":
            log.error("IMAP search failed: %s", status)
            return []

        msg_nums = data[0].split()
        if not msg_nums:
            return []

        log.info("Found %d unread messages", len(msg_nums))
        results = []

        for num in msg_nums:
            status, msg_data = self.conn.fetch(num, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            message_id = msg.get("Message-ID", "")
            if self.state.is_email_processed(message_id):
                continue

            from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
            subject = msg.get("Subject", "")
            date_str = msg.get("Date", "")

            # Only process reply emails (subject starting with "Re:") when
            # only_replies is enabled. This is the resume/continue delivery path.
            if self.cfg.get("only_replies", False) and not re.match(
                r"^\s*re\s*:", subject, re.IGNORECASE
            ):
                log.info("Skipping non-reply email (only_replies enabled): %r", subject)
                self.state.mark_email_processed(message_id, subject, "", "inbound")
                self.conn.store(num, "+FLAGS", "\\Seen")
                continue

            # Extract body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(errors="replace")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="replace")

            # Strip the prefix from subject to get the actual instruction
            clean_subject = subject
            if prefix and subject.lower().startswith(prefix.lower()):
                clean_subject = subject[len(prefix):].strip()
                if clean_subject.startswith(":"):
                    clean_subject = clean_subject[1:].strip()

            # Detect session ID from body or subject.
            # Strip email-quote markers ("> ", ">>", "| ") so replies that quote
            # a "session: ses_xxx" line still resolve to the original session.
            session_id = None
            session_re = re.compile(r"\bsession\s*:\s*(ses_\w+)", re.IGNORECASE)
            for line in (body + "\n" + subject).splitlines():
                stripped = re.sub(r"^\s*(?:>|\||\d+\s*>\s*)+", "", line).strip()
                m = session_re.search(stripped)
                if m:
                    session_id = m.group(1)
                    break

            results.append(
                {
                    "message_id": message_id,
                    "from": from_addr,
                    "subject": clean_subject,
                    "body": body.strip(),
                    "date": date_str,
                    "session_id": session_id,
                    "raw_subject": subject,
                }
            )

            self.state.mark_email_processed(message_id, subject, session_id or "", "inbound")
            # Mark as seen
            self.conn.store(num, "+FLAGS", "\\Seen")

        return results

    def close(self):
        try:
            if self.conn:
                self.conn.close()
                self.conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Email Sender (SMTP)
# ---------------------------------------------------------------------------


class EmailSender:
    def __init__(self, config: dict):
        self.smtp_cfg = config["smtp"]
        self.notify_cfg = config["notify"]

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        msg = email.mime.text.MIMEText(body, "plain")
        msg["From"] = self.smtp_cfg.get("from_address", self.smtp_cfg["username"])
        msg["To"] = to
        msg["Subject"] = subject

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        # Add session ID header for threading
        if session_id:
            msg["X-OC-Session"] = session_id

        try:
            host = self.smtp_cfg["host"]
            port = self.smtp_cfg.get("port", 587)
            use_ssl = self.smtp_cfg.get("use_ssl", port == 465)
            log.info("Sending email to %s via %s:%s (ssl=%s)", to, host, port, use_ssl)

            if use_ssl:
                with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                    server.login(self.smtp_cfg["username"], self.smtp_cfg["password"])
                    server.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as server:
                    if self.smtp_cfg.get("use_tls", True):
                        server.starttls()
                    server.login(self.smtp_cfg["username"], self.smtp_cfg["password"])
                    server.send_message(msg)

            log.info("Email sent to %s", to)
            return True
        except Exception as e:
            log.error("Failed to send email: %s", e)
            return False

    def notify_task_complete(self, session_title: str, output: str, session_id: str | None = None):
        to = self.notify_cfg["recipient_email"]
        max_chars = self.notify_cfg.get("max_output_chars", 4000)
        truncated = output[:max_chars] + ("..." if len(output) > max_chars else "")

        subject = f"[opencode] Task Complete: {session_title}"
        body = (
            f"Session: {session_title}\n"
            f"Status: Complete\n"
            f"Session-ID: {session_id or 'unknown'}\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"\n--- Output ---\n{truncated}\n"
            f"\n--- Reply to continue ---\n"
            f"Reply to this email with instructions to continue the session.\n"
            f"Prefix subject with [opencode] and include session: {session_id} in the body."
        )
        return self.send(to, subject, body, session_id=session_id)

    def notify_error(self, session_title: str, error: str, session_id: str | None = None):
        to = self.notify_cfg["recipient_email"]
        subject = f"[opencode] Error: {session_title}"
        body = (
            f"Session: {session_title}\n"
            f"Status: Error\n"
            f"Session-ID: {session_id or 'unknown'}\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"\n--- Error ---\n{error}\n"
            f"\n--- Reply to continue ---\n"
            f"Reply to this email with instructions to continue the session.\n"
            f"Prefix subject with [opencode] and include session: {session_id} in the body."
        )
        return self.send(to, subject, body, session_id=session_id)

    def notify_idle(self, session_title: str, summary: str, session_id: str | None = None):
        to = self.notify_cfg["recipient_email"]
        max_chars = self.notify_cfg.get("max_output_chars", 4000)
        truncated = summary[:max_chars] + ("..." if len(summary) > max_chars else "")

        subject = f"[opencode] Waiting: {session_title}"
        body = (
            f"Session: {session_title}\n"
            f"Status: Idle - waiting for input\n"
            f"Session-ID: {session_id or 'unknown'}\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"\n--- Current state ---\n{truncated}\n"
            f"\n--- Reply to continue ---\n"
            f"Reply to this email with instructions to continue the session.\n"
            f"Prefix subject with [opencode] and include session: {session_id} in the body."
        )
        return self.send(to, subject, body, session_id=session_id)


# ---------------------------------------------------------------------------
# SSE Monitor
# ---------------------------------------------------------------------------


class SSEMonitor:
    """Monitors opencode server events via SSE (Server-Sent Events)."""

    def __init__(self, client: OpenCodeClient, state: StateDB, sender: EmailSender, config: dict):
        self.client = client
        self.state = state
        self.sender = sender
        self.config = config
        self._running = True
        self._session_states: dict[str, str] = {}  # session_id -> last known status

    def stop(self):
        self._running = False

    def _parse_sse(self, line: str) -> tuple[str, str] | None:
        """Parse a single SSE line. Returns (event_type, data) or None."""
        if line.startswith("event:"):
            return ("event", line[6:].strip())
        if line.startswith("data:"):
            return ("data", line[5:].strip())
        return None

    def _handle_event(self, event_type: str, data: dict):
        """Handle a parsed SSE event."""
        kind = data.get("properties", {}).get("type", event_type)
        session_id = data.get("properties", {}).get("sessionID", "")

        if not session_id:
            return

        notify_cfg = self.config.get("notify", {})

        # Track session state changes
        old_state = self._session_states.get(session_id)
        new_state = kind
        self._session_states[session_id] = new_state

        if old_state == new_state:
            return  # No state change

        log.info("Session %s: %s -> %s", session_id, old_state, new_state)

        # Get session info
        session_info = self.client.get_session(session_id)
        title = "Unknown"
        if isinstance(session_info, dict):
            title = session_info.get("title", session_id)

        self.state.upsert_session(session_id, "", title, new_state)

        # Notify based on state.
        # session.idle at termination doubles as task-complete; notify once,
        # preferring on_task_complete to avoid sending both complete + idle emails.
        last_output = self.client.get_last_output(session_id)
        status_line = f"Status: {kind}"

        if kind == "session.completed" and notify_cfg.get("on_task_complete"):
            self.sender.notify_task_complete(title, last_output or status_line, session_id)

        elif kind == "session.error" and notify_cfg.get("on_error"):
            error_msg = data.get("properties", {}).get("message", "Unknown error")
            self.sender.notify_error(title, error_msg, session_id)

        elif kind == "session.idle":
            if notify_cfg.get("on_task_complete") and old_state not in ("session.completed", "session.idle"):
                self.sender.notify_task_complete(title, last_output or status_line, session_id)
            elif notify_cfg.get("on_idle"):
                self.sender.notify_idle(title, last_output or "Session is idle and waiting for input.", session_id)

    def monitor_global(self):
        """Monitor global SSE events in a blocking loop."""
        log.info("Starting global SSE monitor")
        req = self.client.get_events()

        while self._running:
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    event_type = ""
                    for line_bytes in resp:
                        if not self._running:
                            break
                        line = line_bytes.decode(errors="replace").rstrip("\n\r")

                        if not line:
                            # Empty line = end of event
                            event_type = ""
                            continue

                        parsed = self._parse_sse(line)
                        if not parsed:
                            continue

                        kind, value = parsed
                        if kind == "event":
                            event_type = value
                        elif kind == "data":
                            try:
                                data = json.loads(value)
                                self._handle_event(event_type, data)
                            except json.JSONDecodeError:
                                log.debug("Non-JSON SSE data: %s", value[:100])

            except Exception as e:
                if self._running:
                    log.error("SSE connection error: %s, reconnecting in 5s", e)
                    time.sleep(5)
                    # Re-create request for reconnect
                    req = self.client.get_events()

        log.info("SSE monitor stopped")


# ---------------------------------------------------------------------------
# Main Bridge Orchestrator
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self, config: dict):
        self.config = config
        self.state = StateDB()
        self.client = OpenCodeClient(config["opencode"]["server_url"])
        self.sender = EmailSender(config)
        self.monitor = SSEMonitor(self.client, self.state, self.sender, config)
        self.email_monitor: EmailMonitor | None = None
        self._running = True

    def _handle_email(self, email_data: dict):
        """Process an incoming email and feed it to the appropriate session."""
        session_id = email_data.get("session_id")
        text = email_data.get("body") or email_data.get("subject", "")
        message_id = email_data.get("message_id", "")

        if not text.strip():
            log.warning("Empty email body, skipping")
            return

        if session_id:
            self._resume_session_from_email(session_id, email_data)
        else:
            # No session specified - create new
            self._create_session_from_email(email_data)

    def _resume_session_from_email(self, session_id: str, email_data: dict):
        """Resume an existing session headlessly via `opencode run -s`.

        Runs the session as its own process against the shared SQLite store
        (history is preserved, new turns land in the DB), so a standalone TUI
        attached to the same store stays synced when it reloads the session.
        """
        message_id = email_data.get("message_id", "")
        text = email_data.get("body") or email_data.get("subject", "")

        info = self.client.get_session(session_id)
        # `opencode run -s` resolves the session from the shared DB, so a
        # transient HTTP failure (get_session -> None) need not block resume.
        if info:
            project_dir = info.get("directory") or self.config["opencode"].get("project_dir", ".")
        else:
            log.warning("Could not fetch session %s over HTTP, resuming via DB", session_id)
            project_dir = self.config["opencode"].get("project_dir", ".")
        prompt = (
            f"[Email from {email_data.get('from', 'unknown')} at {email_data.get('date', '')}]\n"
            f"Subject: {email_data.get('raw_subject', '')}\n\n"
            f"{text}"
        )

        cmd = ["opencode", "run", "-s", session_id, "--dir", project_dir, "--format", "json"]
        if self.config["opencode"].get("auto_approve"):
            cmd.append("--auto")
            log.info("auto_approve enabled: passing --auto to resume run")
        cmd.append(prompt)
        log.info("Resuming session %s via: %s ...", session_id, " ".join(cmd[:7]))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=project_dir,
            )
            output = self._extract_run_output(result)
            log.info("Session %s resumed, exit code %d", session_id, result.returncode)

            if result.returncode == 0:
                self.sender.send(
                    self.config["notify"]["recipient_email"],
                    f"[opencode] Session complete: {session_id}",
                    f"Your instructions have been processed in session {session_id}.\n"
                    f"session: {session_id}\n"
                    f"\n--- Output ---\n{output[: self.config['notify'].get('max_output_chars', 4000)]}\n"
                    f"\n--- Reply to continue ---\n"
                    f"Reply to this email with more instructions to continue.",
                    in_reply_to=message_id,
                    session_id=session_id,
                )
            else:
                log.error("Session %s run failed (exit %d): %s", session_id, result.returncode, result.stderr[:300])
                self.sender.send(
                    self.config["notify"]["recipient_email"],
                    f"[opencode] Error: session {session_id}",
                    f"Session {session_id} exited with code {result.returncode}.\n"
                    f"session: {session_id}\n"
                    f"\n--- Error ---\n{result.stderr[:2000]}",
                    in_reply_to=message_id,
                    session_id=session_id,
                )
        except subprocess.TimeoutExpired:
            log.error("Session %s resume timed out", session_id)
            self.sender.send(
                self.config["notify"]["recipient_email"],
                f"[opencode] Timeout: session {session_id}",
                f"Session {session_id} took too long.\nIt may still be running in the background.",
                in_reply_to=message_id,
                session_id=session_id,
            )
        except Exception as e:
            log.error("Failed to resume session %s: %s", session_id, e)

    @staticmethod
    def _extract_run_output(result):
        """Extract the final assistant text from `opencode run --format json` stdout.

        The stream emits text-part events of the form:
            {"type":"text","sessionID":...,"part":{"type":"text","text":...}}
        We collect the last assistant text block.
        """
        texts: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "text":
                part = data.get("part") or {}
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if texts:
            return texts[-1].strip() or result.stdout.strip()[:4000]
        return result.stdout.strip()[:4000]


    def _create_session_from_email(self, email_data: dict):
        """Create a new session from an email."""
        project_dir = self.config["opencode"].get("project_dir", ".")
        text = email_data.get("body") or email_data.get("subject", "")
        title = email_data.get("subject", "Email-initiated task")[:100]

        session_id = self.client.create_session(title=title, project_dir=project_dir)
        if session_id:
            log.info("Created new session %s from email", session_id)
            # Run it headlessly (writes history to the shared store) and email the output.
            self._resume_session_from_email(session_id, email_data)
        else:
            log.error("Failed to create session")
            self.sender.send(
                self.config["notify"]["recipient_email"],
                "[opencode] Session creation failed",
                "Could not create a new opencode session. Check server status.",
                in_reply_to=email_data.get("message_id"),
            )

    def poll_and_process(self):
        """One-shot: poll IMAP and process emails."""
        try:
            self.email_monitor = EmailMonitor(self.config, self.state)
            emails = self.email_monitor.poll()
            self.email_monitor.close()
            self.email_monitor = None

            for em in emails:
                log.info("Processing email: %s from %s", em["subject"], em["from"])
                self._handle_email(em)

            return len(emails)
        except Exception as e:
            log.error("Poll failed: %s", e)
            return 0

    def run_daemon(self):
        """Run as a daemon: SSE monitor + periodic IMAP polling."""
        log.info("Starting opencode-email-bridge daemon")

        # Handle signals
        def shutdown(signum, frame):
            log.info("Received signal %d, shutting down", signum)
            self._running = False
            self.monitor.stop()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        # Start SSE monitor in a thread
        sse_thread = threading.Thread(target=self.monitor.monitor_global, daemon=True)
        sse_thread.start()

        # IMAP polling loop
        poll_interval = self.config["imap"].get("poll_interval_seconds", 30)
        log.info("IMAP poll interval: %ds", poll_interval)

        while self._running:
            try:
                self.poll_and_process()
            except Exception as e:
                log.error("Poll cycle error: %s", e)

            # Sleep in small chunks for responsive shutdown
            for _ in range(poll_interval):
                if not self._running:
                    break
                time.sleep(1)

        log.info("Daemon stopped")

    def show_status(self):
        """Show status of tracked sessions."""
        sessions = self.state.get_active_sessions()
        if not sessions:
            print("No tracked sessions.")
            return

        print(f"\n{'Session ID':<30} {'Title':<40} {'Status':<15} {'Last Activity'}")
        print("-" * 110)
        for s in sessions:
            age = time.time() - s["last_activity"]
            if age < 60:
                age_str = f"{int(age)}s ago"
            elif age < 3600:
                age_str = f"{int(age / 60)}m ago"
            else:
                age_str = f"{int(age / 3600)}h ago"
            print(f"{s['session_id']:<30} {s['title'][:40]:<40} {s['status']:<15} {age_str}")

        # Also check server directly
        try:
            live = self.client.list_sessions()
            if live:
                print(f"\nLive server sessions: {len(live)}")
                for s in live[:10]:
                    sid = s.get("id", "?")
                    title = s.get("title", "?")
                    print(f"  {sid} - {title}")
        except Exception:
            print("\nCould not reach opencode server")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="opencode-email-bridge")
    parser.add_argument(
        "command",
        choices=["serve", "poll", "notify", "status"],
        help="Command to run",
    )
    parser.add_argument("message", nargs="*", help="Message for notify command")
    parser.add_argument("-c", "--config", type=Path, help="Config file path")
    parser.add_argument("--daemon", action="store_true", help="Run serve as background daemon")
    args = parser.parse_args()

    config = load_config(args.config)
    bridge = Bridge(config)

    if args.command == "serve":
        if args.daemon:
            # Fork to background
            pid = os.fork()
            if pid > 0:
                print(f"Bridge daemon started (PID {pid})")
                sys.exit(0)
            os.setsid()
            # Redirect stdio
            sys.stdin = open(os.devnull)
            sys.stdout = open(LOG_PATH, "a")
            sys.stderr = open(LOG_PATH, "a")
        bridge.run_daemon()

    elif args.command == "poll":
        count = bridge.poll_and_process()
        print(f"Processed {count} email(s)")

    elif args.command == "notify":
        msg = " ".join(args.message) if args.message else "Test notification"
        bridge.sender.send(
            bridge.config["notify"]["recipient_email"],
            "[opencode] Test Notification",
            msg,
        )
        print("Notification sent")

    elif args.command == "status":
        bridge.show_status()


if __name__ == "__main__":
    main()
