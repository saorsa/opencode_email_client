#!/usr/bin/env bash
# run-all.sh - Start opencode serve (auth'd), attach a TUI, and run the bridge.
#
# Starts three processes in parallel:
#   1. opencode serve        - headless server on :4096 (basic auth)
#   2. opencode attach       - TUI client attached to that server (same creds)
#   3. bridge.py serve       - the email bridge (uses config.json server_url)
#
# All run as background jobs in this shell; Ctrl-C stops them together.

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
OPENCODE_BIN="${OPENCODE_BIN:-/home/andrey/.opencode/bin/opencode}"
PORT="${PORT:-4096}"

# Auth credentials (override via env or edit here).
SERVE_USER="${SERVE_USER:-opencode}"
SERVE_PASS="${SERVE_PASS:-opencode-local}"

# Directory the server runs in / that the TUI attaches to.
RUN_DIR="${RUN_DIR:-/home/andrey/git/opencode}"

LOG_DIR="$BRIDGE_DIR/logs"
mkdir -p "$LOG_DIR"

echo "opencode binary : $OPENCODE_BIN"
echo "serve port      : $PORT"
echo "auth user       : $SERVE_USER"
echo "run dir         : $RUN_DIR"

# --- 1. Start serve ---------------------------------------------------------
echo "Starting opencode serve on :$PORT ..."
OPENCODE_SERVER_USERNAME="$SERVE_USER" \
OPENCODE_SERVER_PASSWORD="$SERVE_PASS" \
  "$OPENCODE_BIN" serve --port "$PORT" \
  >"$LOG_DIR/serve.log" 2>&1 &
SERVE_PID=$!
echo "  serve pid=$SERVE_PID (log: $LOG_DIR/serve.log)"

# --- 2. Attach TUI ----------------------------------------------------------
echo "Attaching TUI to http://127.0.0.1:$PORT ..."
OPENCODE_SERVER_USERNAME="$SERVE_USER" \
OPENCODE_SERVER_PASSWORD="$SERVE_PASS" \
  "$OPENCODE_BIN" attach "http://127.0.0.1:$PORT" --dir "$RUN_DIR" \
  >"$LOG_DIR/attach.log" 2>&1 &
ATTACH_PID=$!
echo "  attach pid=$ATTACH_PID (log: $LOG_DIR/attach.log)"

# --- 3. Run bridge ----------------------------------------------------------
echo "Starting bridge ..."
python3 "$BRIDGE_DIR/bridge.py" serve \
  >"$LOG_DIR/bridge.log" 2>&1 &
BRIDGE_PID=$!
echo "  bridge pid=$BRIDGE_PID (log: $LOG_DIR/bridge.log)"

echo ""
echo "All started. PIDs: serve=$SERVE_PID attach=$ATTACH_PID bridge=$BRIDGE_PID"
echo "Press Ctrl-C to stop all."

tail -f "$LOG_DIR/bridge.log" "$LOG_DIR/serve.log" "$LOG_DIR/attach.log" &
TAIL_PID=$!

# --- Cleanup ----------------------------------------------------------------
cleanup() {
  echo ""
  echo "Stopping all processes ..."
  kill "$BRIDGE_PID" "$SERVE_PID" "$ATTACH_PID" "$TAIL_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "Done."
}
trap cleanup INT TERM

wait "$BRIDGE_PID" "$SERVE_PID" "$ATTACH_PID" 2>/dev/null || true
