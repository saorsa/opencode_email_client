# opencode-email-bridge

Email ↔ OpenCode bridge. Control opencode sessions via email.

## How it works

```
  You                  Bridge                  OpenCode
   |                     |                       |
   |-- send email ------>|                       |
   |   [opencode] fix   |-- POST /message -----> |
   |                     |<-- SSE events --------|
   |<-- email notify ----|                       |
   |                     |<-- task complete -----|
```

## Quick start

```bash
cd /xxx/opencode-email-bridge
bash setup.sh

# Edit config.json with your IMAP/SMTP credentials
nano config.json

# Test
python3 bridge.py notify "test"
python3 bridge.py status
```

## Two modes

### Mode 1: Server daemon (recommended)

Runs `opencode serve` in background, monitors SSE events, polls IMAP continuously.

```bash
# Start bridge
python3 bridge.py serve

# Or as systemd service
sudo cp opencode-email-bridge@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opencode-email-bridge@$(whoami)
```

### Mode 2: Plugin-based (lightweight)

Uses an opencode plugin for notifications + cron for IMAP polling.

1. Copy the plugin to your project:
```bash
cp hook-notify-plugin.ts /path/to/your-project/.opencode/plugins/email-notify.ts
```

2. Add cron:
```bash
crontab -e
# Add: */2 * * * * python3 /home/andrey/git/opencode-email-bridge/hook_poll.py poll
```

## Email format

**Start new session:**
```
Subject: [opencode] Fix the bug in auth.ts
Body: Fix the login timeout issue
```

**Continue existing session:**
```
Subject: [opencode] Now add tests
Body: session: ses_xxx
Now add unit tests for the fix
```

## Gmail setup

1. Enable 2FA on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Generate an app password for "Mail"
4. Use that password in `config.json`

## Commands

```bash
python3 bridge.py serve          # Run daemon
python3 bridge.py serve --daemon # Run as background process
python3 bridge.py poll           # One-shot IMAP poll
python3 bridge.py notify "msg"   # Send test notification
python3 bridge.py status         # Show tracked sessions
```

## Plugin

The plugin at `hook-notify-plugin.ts` listens for `session.idle` events and calls `hook_notify.py` with session ID and title.

Copy it to your project's `.opencode/plugins/` directory to enable email notifications.

## Configuration

See `config.example.json` for all options. Key settings:

- `imap.*` - IMAP connection settings
- `smtp.*` - SMTP connection settings  
- `notify.*` - Which events trigger notifications
- `opencode.*` - OpenCode server connection
- `session.*` - Session management options

## Credits

Thanks to Big Pickle for the comprehensive review and fixes (session-continue
resume, quoted reply parsing, plugin path, and task-complete output in emails).

