#!/usr/bin/env bash
# setup.sh - Install and configure opencode-email-bridge
set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$BRIDGE_DIR/config.json"
CRON_MARKER="# opencode-email-bridge"

echo "=== opencode-email-bridge setup ==="
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

# Check config
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating config.json from example..."
    cp "$BRIDGE_DIR/config.example.json" "$CONFIG_FILE"
    echo ""
    echo ">>> EDIT config.json with your IMAP/SMTP credentials <<<"
    echo ">>> For Gmail: use App Passwords, not your real password  <<<"
    echo ""
    echo "  1. Enable 2FA on your Google account"
    echo "  2. Go to https://myaccount.google.com/apppasswords"
    echo "  3. Generate an app password for 'Mail'"
    echo "  4. Use that in config.json"
    echo ""
fi

echo ""
echo "=== Mode 1: Server daemon (recommended) ==="
echo ""
echo "  1. Edit config.json with your credentials"
echo "  2. Start the bridge:"
echo "     python3 $BRIDGE_DIR/bridge.py serve"
echo ""
echo "  Or install as systemd service (as root):"
echo "     sudo cp $BRIDGE_DIR/opencode-email-bridge@.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now opencode-email-bridge@$(whoami)"
echo ""

echo "=== Mode 2: Hook-based (lightweight) ==="
echo ""
echo "  1. Edit config.json with your credentials"
echo "  2. Add to your opencode.json under the right project:"
echo ""
cat <<'HOOKJSON'
  "experimental": {
    "hook": {
      "session_completed": [
        {
          "command": [
            "python3",
            "/home/andrey/git/opencode-email-bridge/hook_notify.py",
            "{session_id}",
            "{session_title}"
          ]
        }
      ]
    }
  }
HOOKJSON
echo ""
echo "  3. Add cron entry for IMAP polling:"
echo "     Run: crontab -e"
echo "     Add:"
echo "     */2 * * * * python3 $BRIDGE_DIR/hook_poll.py poll >> $BRIDGE_DIR/bridge.log 2>&1"
echo ""

echo "=== Quick test ==="
echo ""
echo "  python3 $BRIDGE_DIR/bridge.py notify 'Test message from bridge'"
echo "  python3 $BRIDGE_DIR/bridge.py status"
echo ""

echo "=== Email format ==="
echo ""
echo "  To start a NEW session, send email with:"
echo "    Subject: [opencode] <your instruction>"
echo "    Body: <your instruction>"
echo ""
echo "  To CONTINUE an existing session, include in body:"
echo "    Subject: [opencode] <your instruction>"
echo "    Body: session: <session-id>"
echo "    <your instruction>"
echo ""

echo "=== Files ==="
echo ""
echo "  config.json        - IMAP/SMTP/server configuration"
echo "  bridge.py          - Main bridge (server mode)"
echo "  hook_notify.py     - Hook callback for session completion"
echo "  hook_poll.py       - Lightweight cron-based IMAP poller"
echo "  bridge_state.db    - State database (auto-created)"
echo "  bridge.log         - Log file"
echo ""
