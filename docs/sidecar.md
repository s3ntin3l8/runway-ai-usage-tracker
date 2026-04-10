# Universal Sidecar Collector

The **Runway Sidecar** is a lightweight, zero-dependency Python script that collects AI usage metrics from your host machine and pushes them to a Runway instance.

## Quick Start

```bash
# 1. Create config file (auto-created on first run)
python3 scripts/sidecar.py
# Edit ~/.config/runway/sidecar/config.json with your API URL and key

# 2. Test without pushing
python3 scripts/sidecar.py --dry-run

# 3. Run once
python3 scripts/sidecar.py

# 4. Run as daemon (recommended)
python3 scripts/sidecar.py --daemon
```

## Features

- **Zero Dependencies:** Uses only Python Standard Library (`urllib`)
- **Cross-Platform:** Works on macOS, Linux, Windows
- **Daemon Mode:** Persistent process with configurable intervals
- **Offline Queue:** Caches metrics locally when server unreachable
- **Retry Logic:** Exponential backoff for failed pushes
- **PID File:** Prevents multiple daemon instances
- **10 Providers:** Claude, GitHub Copilot, Gemini, ChatGPT, OpenCode, zAI, Kimi, Antigravity
- **HMAC-SHA256 Signing:** Secure payload verification

## Configuration

Config file location:
- **Linux/macOS:** `~/.config/runway/sidecar/config.json`
- **Windows:** `%APPDATA%/runway/sidecar/config.json`

**Required fields:**
```json
{
  "api_url": "http://your-server:8765",
  "api_key": "your-secret-key"
}
```

**Optional fields (with defaults):**
```json
{
  "interval_seconds": 1800,
  "providers": ["all"],
  "retry_attempts": 3,
  "retry_backoff_seconds": 5,
  "queue_max_size_mb": 10,
  "log_level": "INFO",
  "log_file_enabled": true
}
```

## Usage

### One-Shot Mode

Run once and exit:
```bash
python3 scripts/sidecar.py
```

### Daemon Mode

Run continuously with periodic collection:
```bash
python3 scripts/sidecar.py --daemon
```

With custom config:
```bash
python3 scripts/sidecar.py --daemon --config /path/to/config.json
```

**Managing the daemon:**
```bash
# Start in background
python3 scripts/sidecar.py --daemon &

# Check if running
cat ~/.config/runway/sidecar/sidecar.pid

# Stop gracefully
kill $(cat ~/.config/runway/sidecar/sidecar.pid)
```

### Systemd Service (Linux)

Create `/etc/systemd/system/runway-sidecar.service`:
```ini
[Unit]
Description=Runway Sidecar
After=network.target

[Service]
Type=simple
User=%I
ExecStart=/usr/bin/python3 /path/to/scripts/sidecar.py --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable runway-sidecar@$USER
sudo systemctl start runway-sidecar@$USER
```

### LaunchAgent (macOS)

Create `~/Library/LaunchAgents/com.runway.sidecar.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.runway.sidecar</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/scripts/sidecar.py</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/runway-sidecar.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/runway-sidecar.error.log</string>
</dict>
</plist>
```

Load and start:
```bash
launchctl load ~/Library/LaunchAgents/com.runway.sidecar.plist
launchctl start com.runway.sidecar
```

## Supported Providers

| Provider | Data Source | Required Environment |
|----------|-------------|---------------------|
| **Claude** | OAuth token | `CLAUDE_CODE_OAUTH_TOKEN` or `~/.claude/.credentials.json` or macOS keychain |
| **GitHub Copilot** | API token | `GITHUB_TOKEN` or Windows Credential Manager |
| **Gemini** | OAuth + logs | `~/.gemini/oauth_creds.json` |
| **ChatGPT** | OAuth + logs | `~/.codex/auth.json` |
| **OpenCode** | SQLite DB | `~/.local/share/opencode/opencode.db` or Chrome cookie |
| **zAI API/Plan** | API key | `ZAI_API_KEY` |
| **Kimi API** | API key | `KIMI_API_KEY` |
| **Kimi Coding** | JWT/cookie | `KIMI_AUTH_TOKEN` or Chrome cookie |
| **Antigravity** | JSON file | `~/.antigravity/state/quota.json` |

## Deployment Modes

### Standalone
Runway and sidecar on same machine:
```bash
python3 scripts/sidecar.py --daemon
```

### Multi-Host
Main PC runs Runway, laptops send data:
```bash
# On laptop
python3 scripts/sidecar.py --daemon
```

### Docker
Runway in container, workstations send data:
```bash
# Server
docker run -p 8765:8765 -e INGEST_API_KEY=secret runway

# Each workstation
python3 scripts/sidecar.py --daemon
```

See [Deployment Modes Guide](deployment-modes.md) for complete setup.

## Offline Queue

When the server is unreachable, metrics are stored locally:

- **Location:** `~/.config/runway/sidecar/queue/YYYY-MM-DD.jsonl`
- **Format:** JSON Lines with timestamp and payload
- **Rotation:** FIFO, oldest files removed when >10MB total
- **Replay:** Automatically sent when connection restored

Example queue file:
```jsonl
{"ts": 1712581200, "payload": {"provider": "sidecar-laptop", "metrics": [...]}}
{"ts": 1712581300, "payload": {"provider": "sidecar-laptop", "metrics": [...]}}
```

## Token Transmission Architecture

```
+--------------+     Signed       +--------------+
|   Sidecar    | ---------------> |    Server    |
|  (Workstation)|  HMAC-SHA256    |  (Runway)    |
|              |                  |              |
| - Files      | ---------------> | - Signature  |
| - Keychain   |     POST         |   Verification
| - Cookies    |   /api/ingest    | - API Calls  |
+--------------+                  +--------------+
```

**Flow:**
1. Sidecar extracts tokens from local files/keychain
2. Signs payload with `api_key` using HMAC-SHA256
3. Sends to server via `POST /api/ingest`
4. Server verifies signature, stores tokens in memory cache (30-min TTL)
5. Server makes API calls using cached tokens

**Security:**
- Tokens stored in memory only (no disk persistence on server)
- Lost on server restart, refreshed by sidecar on next run
- Server does all API calls, sidecar only extracts/pushes

## Logging

Logs are written to both console and file (if enabled):
- **File:** `~/.config/runway/sidecar/sidecar.log`
- **Rotation:** Manual (log file grows until cleared)

View logs:
```bash
# Follow log
tail -f ~/.config/runway/sidecar/sidecar.log

# Verbose mode (one-shot)
python3 scripts/sidecar.py --verbose --dry-run
```

## Troubleshooting

### Sidecar not collecting
```bash
# Test specific provider
python3 scripts/sidecar.py --provider anthropic --dry-run --verbose

# Check env vars
env | grep -E "(GITHUB|ZAI|KIMI|CLAUDE)"
```

### Push failures
- Verify API URL is reachable: `curl http://server:8765/api/health`
- Check config has correct `api_key`
- View logs: `tail -f ~/.config/runway/sidecar/sidecar.log`
- Check queue: `ls -la ~/.config/runway/sidecar/queue/`

### Daemon not starting
- Check PID file: `cat ~/.config/runway/sidecar/sidecar.pid`
- Kill stale process: `kill $(cat ~/.config/runway/sidecar/sidecar.pid)`
- Remove PID file manually if needed: `rm ~/.config/runway/sidecar/sidecar.pid`

### Multiple instances
The sidecar uses a PID file to prevent multiple daemons. If the sidecar crashed:
```bash
# Remove stale PID file
rm ~/.config/runway/sidecar/sidecar.pid

# Restart
python3 scripts/sidecar.py --daemon
```

---

See [Collector Docs](../docs/collectors/) for provider-specific setup details.

*Last updated: 2026-04-10*
