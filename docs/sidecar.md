# Universal Sidecar Collector

## Desktop App (macOS, Windows, Linux)

### Download

Download the latest release from the [GitHub Releases page](https://github.com/s3ntin3l8/runway/releases/latest):
- **macOS**: `Runway-Sidecar-macOS.zip` → unzip → drag `Runway Sidecar.app` to `/Applications`
- **Windows**: `Runway-Sidecar-Windows.zip` → unzip → run `RunwaySidecar.exe`
- **Linux (desktop tray)**: `Runway-Sidecar-Linux.tar.gz` → `tar -xzf …` → run `./RunwaySidecar`. Requires a tray host (AppIndicator on GNOME/Unity, GTK on KDE/Xfce) and a DBus session. For headless servers / Docker, use the CLI binary below instead.
- **Linux (headless CLI)**: `Runway-Sidecar-Linux-CLI.tar.gz` → `tar -xzf …` → run `./runway-sidecar-cli --daemon`. Single-file binary, no Python install needed, no GUI dependencies. Use this on servers, in Docker, and on CI agents.

Each release asset ships a matching `*.sha256` checksum file. Verify with `shasum -a 256 -c <file>.sha256` (macOS/Linux).

### Edge builds (rolling, Linux)

Edge is the sidecar analog of the Docker `:edge` image — a rolling build published on every push to `main` that touches sidecar code. It lives in a single, always-overwritten `edge` **prerelease**, so the download URLs are stable:

- **Linux (desktop tray)**: <https://github.com/s3ntin3l8/runway/releases/download/edge/Runway-Sidecar-Linux-edge.tar.gz>
- **Linux (headless CLI)**: <https://github.com/s3ntin3l8/runway/releases/download/edge/Runway-Sidecar-Linux-CLI-edge.tar.gz>

```bash
# Deploy the latest edge CLI sidecar to another Linux box:
curl -fsSL -O https://github.com/s3ntin3l8/runway/releases/download/edge/Runway-Sidecar-Linux-CLI-edge.tar.gz
curl -fsSL -O https://github.com/s3ntin3l8/runway/releases/download/edge/Runway-Sidecar-Linux-CLI-edge.tar.gz.sha256
shasum -a 256 -c Runway-Sidecar-Linux-CLI-edge.tar.gz.sha256
tar -xzf Runway-Sidecar-Linux-CLI-edge.tar.gz
./runway-sidecar-cli --daemon
```

Edge binaries report their version as `<base>+edge.<short-sha>` (e.g. `1.1.0+edge.abc1234`); `--version` shows the exact build. Because the `edge` release is a prerelease, it is **never** returned by GitHub's "latest release" API, so stable sidecars and the dashboard's "update available" flag ignore it entirely. To have edge sidecars notified when a newer edge build lands, set the update channel to **Edge** (see below).

### First Run

On first launch, if no config file exists, the app creates a template config and opens it in the default editor.

The config is located at:
- **macOS/Linux**: `~/.config/runway/sidecar/config.json`
- **Windows**: `%APPDATA%\runway\sidecar\config.json`

Set the following required fields:
- `api_url`: The address of your Runway server (e.g., `http://localhost:8765` for local, or `https://your-server.com:8765` for remote)
- `api_key`: The HMAC key from your Runway instance's Fleet settings page

Restart the app after editing the config for changes to take effect.

### Unsigned Binary Warning

**macOS (Gatekeeper):**

The app is not code-signed. On first launch, right-click `Runway Sidecar.app` → **Open**, then click **Open** in the dialog that appears to bypass Gatekeeper.

**Windows (SmartScreen):**

Click **More info** → **Run anyway** to bypass SmartScreen on first launch.

### Tray / Menubar

The sidecar runs as a background app with a menu icon showing its status:

**Icon Color:**
- **Green**: All systems healthy
- **Amber**: Warning or stale data
- **Red**: Error or config needed
- **Grey**: Paused

**Menu Items:**
- **Open Dashboard**: Launch the Runway web interface
- **Run Now**: Trigger an immediate collection cycle
- **Pause / Resume**: Temporarily stop or restart collection
- **Launch at Login**: Register the app to start automatically on system boot
- **Edit Config**: Open the config file in your default editor
- **View Logs**: Open the log file for debugging
- **Check for Updates…**: Open the releases page to download a newer version
- **Quit**: Exit the app

### Automatic Startup

Click **Launch at Login** in the menu to register the sidecar as a login item (macOS) or startup task (Windows). Click again to remove it.

### Updates

The sidecar checks for updates daily. When a newer version is available, the menu title shows **(update available)**. Click **Check for Updates…** to open the releases page and download the latest version.

---

## Headless / CLI Mode (Linux, Advanced Users)

> The sections below describe running the sidecar as a headless script or system daemon. This is the recommended approach on Linux and for server/Docker deployments.

The **Runway Sidecar** is a lightweight, zero-dependency Python script that collects AI usage metrics from your host machine and pushes them to a Runway instance.

You can run the same daemon two ways:

1. **`scripts/sidecar.py`** — invoke the Python source directly. Best when you already have a Python toolchain checked out.
2. **`runway-sidecar-cli`** — the precompiled single-file binary from `Runway-Sidecar-Linux-CLI.tar.gz`. Drop it into a slim container or a server with no Python install. Every flag below works against either entry point — substitute `./runway-sidecar-cli` for `python3 scripts/sidecar.py`.

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
- **13 Providers:** Claude, GitHub Copilot, Gemini, ChatGPT, OpenRouter, MiniMax, OpenCode, Ollama, zAI, Kimi, Kimi K2 (Antigravity is sidecar-only — see [collector doc](collectors/antigravity.md))
- **Event Batching:** Per-message events (Claude, Codex, Gemini, OpenCode) shipped in 1000-event batches
- **Persistent Watermark:** `~/.config/runway-sidecar/event-watermark.json` tracks last-pushed timestamp per (provider, account)
- **HMAC-SHA256 Signing:** Secure payload verification

## Configuration

Config file location:
- **Linux/macOS:** `~/.config/runway/sidecar/config.json`
- **Windows:** `%APPDATA%/runway/sidecar/config.json`

**Custom Config Directory**:
The default location for Runway's (and Sidecar's) configuration files is platform-specific. You can override this location by setting the `RUNWAY_CONFIG_DIR` environment variable to an absolute path. For example, if `RUNWAY_CONFIG_DIR` is set to `/opt/runway`, then the sidecar config will be expected at `/opt/runway/sidecar/config.json`.

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
  "retry_attempts": 3,
  "retry_backoff_seconds": 5,
  "queue_max_size_mb": 10,
  "log_level": "INFO",
  "log_file_enabled": true
}
```

The sidecar's polling cadence is server-controlled (via the `poll_providers` field returned from `/api/v1/fleet/ingest`); there is no local `interval_seconds` or `providers` config. Configure per-provider intervals and enable/disable in the Runway dashboard's fleet settings.

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
| **Claude** | OAuth / cookie / file | `CLAUDE_CODE_OAUTH_TOKEN`, `~/.claude/.credentials.json`, macOS keychain, `sessionKey` cookie, or `~/.claude/statusline.json` |
| **GitHub Copilot** | API token, OAuth | `GITHUB_TOKEN` (from .env or OAuth flow), `gh` CLI (from `~/.config/gh/hosts.yml`), or Windows Credential Manager |
| **Gemini** | OAuth | `~/.gemini/oauth_creds.json` |
| **ChatGPT** | OAuth / cookie | `CHATGPT_OAUTH_TOKEN`, `~/.codex/auth.json`, or Chrome cookie |
| **OpenCode** | SQLite DB | `~/.local/share/opencode/opencode.db` or Chrome cookie |
| **zAI API/Plan** | API key | `ZAI_API_KEY` |
| **Kimi API** | API key | `KIMI_API_KEY` |
| **Kimi Coding** | JWT/cookie | `KIMI_AUTH_TOKEN` or Chrome cookie |
| **OpenRouter** | API key | `OPENROUTER_API_KEY` |
| **MiniMax** | API key | `MINIMAX_API_KEY` |
| **Ollama** | Session cookie | `OLLAMA_SESSION_TOKEN` or browser cookie |
| **Kimi K2** | API key | `KIMI_K2_API_KEY` |
| **Antigravity** | LSP probe / JSON file (sidecar-only) | LSP process (running IDE) or `~/.local/share/antigravity/state/quota.json` |

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

See [Deployment Guide](deployment.md) for complete setup.

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
| - Cookies    | /api/v1/fleet/   | - API Calls  |
|              |   ingest         |              |
+--------------+                  +--------------+
```

**Flow:**
1. Sidecar extracts tokens, cards, and per-message events from local files/keychain/IDE logs
2. Signs payload with `api_key` using HMAC-SHA256
3. Sends to server via `POST /api/v1/fleet/ingest`
4. Server verifies signature, stores tokens in memory cache (30-min TTL), upserts cards into `latest_usage`, and ingests events into `usage_events` (deduped by `(provider_id, account_id, event_id)`)
5. Server makes any required API calls using cached tokens

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
- Verify API URL is reachable: `curl http://server:8765/api/v1/system/health`
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

*Last updated: 2026-05-09*
