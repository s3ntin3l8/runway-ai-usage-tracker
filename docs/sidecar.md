# Universal Sidecar Collector

## Desktop App (macOS, Windows, Linux)

### Download & install

Grab the latest build from the [GitHub Releases page](https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/latest) (the Runway dashboard's **Fleet** page also links the right installer for your OS). `<version>` below is the release tag, e.g. `v2.13.0`.

| Platform | Download | Install |
|---|---|---|
| **macOS** (Apple Silicon) | `Runway-Sidecar-macOS-<version>.dmg` | Open the DMG and drag **Runway Sidecar** onto **Applications**, then launch it from Applications (first launch: see *Unsigned app warning* below). |
| **Windows** | `Runway-Sidecar-Windows-<version>-setup.exe` | Run the installer. It installs per-user (no admin prompt) into `%LOCALAPPDATA%\Programs\Runway Sidecar`, adds Start Menu entries, and offers **Start automatically when I sign in** on the last page. |
| **Linux (desktop tray)** | `Runway-Sidecar-Linux-<version>.tar.gz` | `tar -xzf …` → run `./RunwaySidecar`. Requires a tray host (AppIndicator on GNOME/Unity, GTK on KDE/Xfce) and a DBus session. For headless servers / Docker, use the CLI binary instead. |
| **Linux (headless CLI)** | `Runway-Sidecar-Linux-CLI-<version>.tar.gz` | `tar -xzf …` → run `./runway-sidecar-cli --daemon`. Single-file binary, no Python or GUI dependencies. Use this on servers, in Docker, and on CI agents. |

The release also carries `Runway-Sidecar-macOS-<version>.zip` and `Runway-Sidecar-Windows-<version>.zip`: portable builds (unzip and run, no installer) that the sidecar's self-updater downloads. You normally never need them.

**Windows silent install** (fleet rollout / scripting):

```powershell
.\Runway-Sidecar-Windows-v2.13.0-setup.exe /S /AUTOSTART=1   # /D=C:\path overrides the install dir (must be last)
```

**Uninstalling:**
- **macOS**: quit the app from the menu bar, turn off **Launch at Login** first if you enabled it, then drag `Runway Sidecar.app` from Applications to the Trash.
- **Windows**: *Settings → Apps → Runway Sidecar → Uninstall* (or *Uninstall Runway Sidecar* in the Start Menu). The uninstaller also removes the login item. Silent: `"%LOCALAPPDATA%\Programs\Runway Sidecar\uninstall.exe" /S`.

Neither removes your config, offline queue or logs (`~/.config/runway/sidecar`, `%APPDATA%\runway\sidecar`), so reinstalling picks up where you left off. Delete that folder by hand for a clean slate.

### Verify your download

Every asset has a sibling `<asset>.sha256`, and each release has one `SHA256SUMS.txt` covering all of them:

```bash
sha256sum -c --ignore-missing SHA256SUMS.txt      # Linux   (macOS: shasum -a 256 -c …)
```

Each asset and `SHA256SUMS.txt` is also signed with [Sigstore](https://www.sigstore.dev/) keyless signing from the release workflow (`<asset>.sig` + `<asset>.cert`). To prove a file was built by this repository's CI:

```bash
cosign verify-blob \
  --signature Runway-Sidecar-macOS-v2.13.0.dmg.sig \
  --certificate Runway-Sidecar-macOS-v2.13.0.dmg.cert \
  --certificate-identity-regexp '^https://github\.com/s3ntin3l8/runway-ai-usage-tracker/\.github/workflows/sidecar-build\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  Runway-Sidecar-macOS-v2.13.0.dmg
```

### Edge builds (rolling)

Edge is the sidecar analog of the Docker `:edge` image — a rolling build published on every push to `main` that touches sidecar code. It lives in a single, always-overwritten `edge` **prerelease**, so the download URLs are stable:

- **macOS**: <https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/edge/Runway-Sidecar-macOS-edge.dmg>
- **Windows**: <https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/edge/Runway-Sidecar-Windows-edge-setup.exe>
- **Linux (desktop tray)**: <https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/edge/Runway-Sidecar-Linux-edge.tar.gz>
- **Linux (headless CLI)**: <https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/edge/Runway-Sidecar-Linux-CLI-edge.tar.gz>

(The portable `Runway-Sidecar-{macOS,Windows}-edge.zip` payloads sit alongside.)

```bash
# Deploy the latest edge CLI sidecar to another Linux box:
curl -fsSL -O https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/edge/Runway-Sidecar-Linux-CLI-edge.tar.gz
curl -fsSL -O https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/edge/Runway-Sidecar-Linux-CLI-edge.tar.gz.sha256
sha256sum -c Runway-Sidecar-Linux-CLI-edge.tar.gz.sha256
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

### Unsigned app warning

The sidecar is not signed with a paid Apple Developer ID or Windows code-signing certificate, so each OS asks once before the first launch.

**macOS (Gatekeeper):** the app is ad-hoc signed but not notarized. The first time, right-click **Runway Sidecar** in Applications → **Open**, then **Open** in the dialog (on macOS 15+: try to open it once, then *System Settings → Privacy & Security → Open Anyway*). Launch it from **Applications**, not from inside the DMG window: a copy running off the disk image can't update itself or register Launch at Login, and the sidecar tells you so.

**Windows (SmartScreen):** on the installer's blue *Windows protected your PC* screen, click **More info** → **Run anyway**.

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
- **Check for Updates…**: Open the releases page to download a newer version manually
- **Download & Install Update**: Appears only when an update is available — downloads, verifies, installs, and relaunches in place
- **Roll Back to vX.Y.Z**: Appears only after a self-update. It restores the build that update replaced and relaunches.
- **Quit**: Exit the app

### Automatic Startup

Click **Launch at Login** in the menu to register the sidecar as a login item (macOS LaunchAgent, Windows `HKCU\…\Run` entry, Linux XDG autostart). Click again to remove it. On Windows this is the same setting as the installer's *Start automatically when I sign in* checkbox, so the two always agree.

### Updates

The sidecar checks for updates daily. When a newer version is available, the menu title shows **(update available)**.

You can install the update without leaving the app:
- **Tray app**: click **Download & Install Update** (shown once an update is detected). It downloads the matching release asset, verifies its `.sha256` checksum, swaps the binary/`.app`, and relaunches.
- **Headless CLI**: run `runway-sidecar-cli --self-update` (alias `--update`) for a one-shot download → verify → install, then relaunch under your supervisor (systemd/launchd).
- **Background auto-install**: enable it locally or fleet-wide (see below). The daily check then self-installs newer builds automatically.
- **Push from the dashboard**: on the Fleet page, a sidecar with an available update shows an **Update now** button — clicking it makes that sidecar self-install on its next heartbeat.

**Auto-update control (local vs server):**
- **Server (fleet-wide):** the dashboard's *System → Auto-install updates* toggle (default off) is pushed to every sidecar on its next heartbeat.
- **Local (per-machine):** an explicit `"auto_update": true|false` in `config.json` **overrides** the server toggle — `true` always auto-installs, `false` never does. Omit the key (the default) to defer to the server toggle. This preserves per-machine consent: a machine can hard-opt-out regardless of the fleet setting.

**Constraints & safety:**
- Self-update only runs for the packaged (PyInstaller) binaries. **From-source runs (`python3 scripts/sidecar.py`) and Docker containers are notify-only** — update them with `git pull` / by repulling the image.
- The checksum is **mandatory**: a missing or mismatched `.sha256` aborts the install, leaving the running copy untouched.
- **Rollback:** the build an update replaced is kept next to the install as `<name>.previous` (e.g. `Runway Sidecar.app.previous`, `RunwaySidecar.exe.previous`), with its version in `<name>.previous.version`. If a new build misbehaves, use the tray's **Roll Back to vX.Y.Z** item (shown only when a backup exists) or `runway-sidecar-cli --rollback`. A rollback keeps the newer build as the backup, so you can undo it. Only one backup is kept.
- On Windows, installs made with the setup.exe also get their *Apps & Features* version refreshed after each self-update or rollback.
- Self-update always downloads the portable `.zip` / `.tar.gz` payload, never the `.dmg` / `-setup.exe`, and swaps it in place. It works on every platform and both channels.
- If the install path isn't writable (e.g. `/Applications` for a non-admin macOS account, or `/usr/local/bin`), the update is skipped with a log message and you install the new DMG / setup.exe via **Check for Updates…**. The Windows installer's per-user location is always writable.

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
- **13 Providers:** Claude, GitHub Copilot, Gemini, ChatGPT, OpenRouter, MiniMax, OpenCode, Ollama, zAI, Kimi, Kimi K2, Antigravity (quota via server-side API collector; sidecar ships the OAuth token and token/cost enrichment only)
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
  "log_file_enabled": true,
  "auto_update": false
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
| **OpenCode** | SQLite DB | `~/.local/share/opencode/opencode.db`, `~/.opencode/opencode.db`, or Chrome cookie |
| **zAI API/Plan** | API key | `ZAI_API_KEY` |
| **Kimi API** | API key | `KIMI_API_KEY` |
| **Kimi Coding** | API key / CLI credential / cookie | `KIMI_CODE_API_KEY`, `~/.kimi-code/credentials/kimi-code*.json` (access token + expiry only, never the refresh token), or `KIMI_AUTH_TOKEN` / Chrome cookie |
| **OpenRouter** | API key | `OPENROUTER_API_KEY` |
| **MiniMax** | API key | `MINIMAX_API_KEY` |
| **Ollama** | Session cookie | `OLLAMA_SESSION_TOKEN` or browser cookie |
| **Kimi K2** | API key | `KIMI_K2_API_KEY` |
| **Antigravity** | OAuth token (server-side API collector) | sidecar scrapes `~/.gemini/antigravity-cli/antigravity-oauth-token` and ships it to the server; server queries the Code Assist cloud API for quota. Sidecar emits only token/cost enrichment (never quota). |

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

### TLS / certificate errors (`CERTIFICATE_VERIFY_FAILED`)
The error `<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>` on an `https://`
`api_url` means the sidecar couldn't verify the server's certificate against a
trusted CA. Builds from **v…+** bundle [`certifi`](https://pypi.org/project/certifi/),
so a valid public (e.g. Let's Encrypt) cert verifies out of the box. If you hit
this:

- **Older frozen build (macOS), valid public cert** — point the bundled OpenSSL
  at the system CA store, then relaunch the app:
  ```sh
  launchctl setenv SSL_CERT_FILE /etc/ssl/cert.pem
  ```
- **Self-signed / internal-CA server** — give the sidecar your CA chain:
  - config `~/.config/runway/sidecar/config.json`: `"ca_bundle": "/path/to/ca.pem"`
  - or env: `RUNWAY_CA_BUNDLE=/path/to/ca.pem` (`SSL_CERT_FILE` also honoured)
- **Last resort (trusted network only)** — disable verification entirely:
  config `"tls_insecure": true` or env `RUNWAY_INSECURE=1`. HMAC still
  authenticates payloads, but the channel is no longer confidential — prefer a
  real CA bundle.

These knobs apply only to the push to *your* Runway server; the sidecar's GitHub
self-update always verifies GitHub's public cert regardless.

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
