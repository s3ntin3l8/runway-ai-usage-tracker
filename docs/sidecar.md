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

Each asset and `SHA256SUMS.txt` is also signed with [Sigstore](https://www.sigstore.dev/) keyless signing from the release workflow (`<asset>.sigstore.json` bundle, Cosign 3 format). To prove a file was built by this repository's CI:

```bash
cosign verify-blob \
  --bundle Runway-Sidecar-macOS-v2.13.0.dmg.sigstore.json \
  --certificate-identity https://github.com/s3ntin3l8/runway-ai-usage-tracker/.github/workflows/sidecar-build.yml@refs/heads/main \
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

### Beta releases (numbered)

Beta releases use versioned prerelease tags such as `v3.0.0-beta.1` and `v3.0.0-beta.2`. The Fleet download card's **Beta** option always selects the newest numbered beta release. To have installed sidecars follow that sequence, select **Beta** under *System → Sidecar update channel*. Stable releases remain the default channel.

### First Run

**Easiest: pair from the dashboard.** In Runway, go to *Fleet → Add sidecar → Generate pairing link* and click **Open in Runway Sidecar**. The sidecar opens a confirmation page naming your server. Check the address and click **Pair**. The sidecar fetches the server URL and ingest key itself and starts reporting straight away. Codes are one-time and expire after 10 minutes.

- If the link doesn't open the app (e.g. a Linux tray build, or the browser blocks it): open **Settings…** from the tray icon → **Pair with a code…** and enter the server address and code shown in the dashboard.
- Headless / CLI: `runway-sidecar-cli --pair https://your-server CODE` (or pass the whole `runway-sidecar://pair?…` link). It writes the config and exits; then (re)start the daemon.
- Only pair with a link you just generated in **your own** dashboard: pairing points this machine's usage data and provider sign-ins at that server.

**Manual setup:** on first launch, if no config file exists, the app creates a template config and opens it in the default editor.

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
- **Check for Updates…**: Check the selected update channel now and report whether an update is available or the check failed
- **View Releases…**: Open the releases page for a manual download
- **Download & Install Update**: Appears only when an update is available — downloads, verifies, installs, and relaunches in place
- **Roll Back to vX.Y.Z**: Appears only after a self-update. It restores the build that update replaced and relaunches.
- **Quit**: Exit the app

### Automatic Startup

Click **Launch at Login** in the menu to register the sidecar as a login item (macOS LaunchAgent, Windows `HKCU\…\Run` entry, Linux XDG autostart). Click again to remove it. On Windows this is the same setting as the installer's *Start automatically when I sign in* checkbox, so the two always agree.

### Updates

The sidecar checks for updates daily. When a newer version is available, the menu title shows **(update available)**.
Use **Check for Updates…** to check immediately without installing. If the check finds
an update, **Download & Install Update** appears; **View Releases…** opens the
manual download page.
Manual check attempts, including failed lookups, run at most once a minute; repeated
clicks show an in-progress or cooldown notice. The scheduled daily checker runs independently.

You can install the update without leaving the app:
- **Tray app**: click **Download & Install Update** (shown once an update is detected). It downloads the matching release asset, verifies its `.sha256` checksum and mandatory Sigstore `.sigstore.json` bundle, swaps the binary/`.app`, and relaunches.
- **Headless CLI**: run `runway-sidecar-cli --self-update` (alias `--update`) for a one-shot download → verify → install, then relaunch under your supervisor (systemd/launchd).
- **Background auto-install**: enable it locally or fleet-wide (see below). The daily check then self-installs newer builds automatically.
- **Push from the dashboard**: on the Fleet page, a sidecar with an available update shows an **Update now** button — clicking it makes that sidecar self-install on its next heartbeat.

Updates refuse missing or invalid signature bundles before extraction. Older checksum-only releases cannot be installed through self-update. Verification pins the repository signing workflow on `main` and the GitHub issuer; trust/network failures leave the installed copy unchanged. For a read-only packaging check, use `--verify-update <archive> --bundle <bundle>`.

Remote server URLs require HTTPS; canonical loopback HTTP remains supported. Signed requests and pairing never follow redirects. Explicit insecure HTTPS certificate verification remains available with warnings. See [SECURITY.md](SECURITY.md) for the trust policy.

**Auto-update control (local vs server):**
- **Server (fleet-wide):** the dashboard's *System → Auto-install updates* toggle (default off) is pushed to every sidecar on its next heartbeat.
- **Per sidecar (dashboard):** a sidecar's **Settings** dialog (Fleet → Settings) has an **Updates** section with **Auto-update** (*Follow fleet* / *Always on* / *Always off*) and **Update channel** (*Follow fleet* / *Stable* / *Beta* / *Edge*), so one machine can be pinned without touching the others. The server sends the sidecar's own value in place of the fleet's on its next heartbeat (`sidecar_auto_update` / `sidecar_update_channel` in the ingest response), and "update available" is judged against the channel the sidecar is told to follow. The sidecar reports what it effectively uses (`auto_update`, `update_channel`); when that differs from what Runway asks for, the dialog says so — either the change has not landed yet, or the next bullet applies. Builds that cannot replace themselves (from source, Docker) show the auto-update control as unsupported.
- **Local (per-machine):** an explicit `"auto_update": true|false` in `config.json` **overrides** the server toggle and the dashboard's per-sidecar override — `true` always auto-installs, `false` never does. Omit the key (the default) to defer to the server toggle. This preserves per-machine consent: a machine can hard-opt-out regardless of the fleet setting.

**Constraints & safety:**
- Self-update only runs for the packaged (PyInstaller) binaries. **From-source runs (`python3 scripts/sidecar.py`) and Docker containers are notify-only** — update them with `git pull` / by repulling the image.
- The checksum is **mandatory**: a missing or mismatched `.sha256` aborts the install, leaving the running copy untouched.
- **Rollback:** the build an update replaced is kept next to the install as `<name>.previous` (e.g. `Runway Sidecar.app.previous`, `RunwaySidecar.exe.previous`), with its version in `<name>.previous.version`. If a new build misbehaves, use the tray's **Roll Back to vX.Y.Z** item (shown only when a backup exists) or `runway-sidecar-cli --rollback`. A rollback keeps the newer build as the backup, so you can undo it. Only one backup is kept.
- On Windows, installs made with the setup.exe also get their *Apps & Features* version refreshed after each self-update or rollback.
- Self-update always downloads the portable `.zip` / `.tar.gz` payload, never the `.dmg` / `-setup.exe`, and swaps it in place. It works on every platform and all release channels.
- If the install path isn't writable (e.g. `/Applications` for a non-admin macOS account, or `/usr/local/bin`), the update is skipped with a log message and you install the new DMG / setup.exe via **View Releases…**. The Windows installer's per-user location is always writable.
- **Restart under systemd:** the headless CLI restarts itself after an update. When its systemd unit has `Restart=always` (or `on-failure`) and the CLI is the unit's main process, it exits and lets systemd start it again (`RestartSec` applies), which leaves a clean process tree and frees the old runtime. This also works when an update by an older build left a chain of idle bootloaders between the unit and the daemon: the next update exits through the whole chain and systemd starts a clean tree. Without that supervisor (or with `Restart=no`) it re-executes in place instead, and the previous bootloader process stays idle until the next restart. Those idle processes pile up, one per update at about 2 MB each, so run unattended installs under a unit with `Restart=always` or restart them occasionally.
- **Temp files:** a packaged Linux/Windows build unpacks into a `_MEI*` directory under its temp dir (`TMPDIR`, e.g. `/opt/runway-sidecar/tmp` in the example unit) and removes it on a clean exit. A killed process cannot, so at startup the sidecar deletes `_MEI*` directories whose recorded owner process is gone (only ones it marked itself), `runway-update-*` download folders older than an hour, and finishes a rollback that was interrupted. Directories left by builds from before this check are not marked and are never touched; delete them by hand once.

---

Memory measurements and reproducible comparisons are documented in
[Sidecar memory measurements](sidecar-memory.md).

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
- **Event Batching:** Per-message events (Claude, Codex, Gemini, OpenCode, MiniMax Code) shipped in 1000-event batches
- **Persistent Watermark:** `~/.config/runway-sidecar/event-watermark.json` tracks last-pushed timestamp per (provider, account). The MiniMax Code extractor keeps an id-based cursor of its own in `minimax_code_watermark.json` next to the sidecar's state, because the runtime's request timestamps are not globally monotonic — see [`docs/collectors/minimax.md`](collectors/minimax.md#minimax-code-traffic)
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
  "auto_update": false,
  "keep_alive": false,
  "claude_config_dirs": [],
  "codex_home": []
}
```

**Log level from the dashboard.** `log_level` is read once at startup, but you can raise it without touching the machine: a sidecar's **Settings** dialog (Fleet → Settings) has a **Logging** section with a **Log level** list (*Use sidecar config* / Debug / Info / Warning / Error). The server sends it as `log_level` on every check-in and the sidecar applies it immediately, no restart; read the output with **Logs** on the card. *Use sidecar config* (or **Reset** under a Debug banner) puts the `config.json` level back. The override is not written to the config, so a config rewrite cannot undo it, and it stays until you reset it — the card shows a `debug logging` badge meanwhile. The sidecar reports the level it logs at (`log_level`); builds that predate this (they report nothing) get an explanation and an **Update it** button instead of the control. The tray app takes the override too; its own base level is INFO.

**Logins in a custom directory.** Claude Code relocates its whole state (`.credentials.json`, `.claude.json`, `projects/`) when `CLAUDE_CONFIG_DIR` is set, and Codex does the same with `CODEX_HOME` (`auth.json`, `sessions/`). The sidecar searches those directories **in addition to** `~/.claude` / `~/.codex`: the env var (comma-separated) plus the `claude_config_dirs` / `codex_home` config keys (a list or a comma-separated string). Use the config keys when the sidecar runs as a service (systemd, launchd, Task Scheduler), because a service does not inherit your shell's environment. Each Claude directory's `.credentials.json` is paired with the `.claude.json` beside it for the account email. The tray app reads the same config keys (and re-reads them when you save or reload the config).

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

### Keep-alive for Antigravity, Claude Code, Codex and xAI (opt-in)

Four logins are renewed only by their own CLI, so on hosts where that CLI runs intermittently the quota card lapses and goes stale. One opt-in flag keeps all of them alive:

```bash
python3 scripts/sidecar.py --daemon --keep-alive
# or set "keep_alive": true in config.json
```

The sidecar then checks the local agy token every minute and, once the token has lapsed, runs `agy models` — a metadata call that renews the access token in place (the refresh token is never rotated, no model call is made). Failures back off to one attempt every five minutes and are logged as warnings; the thread never takes the sidecar down. The token is read from wherever this OS keeps it: `~/.gemini/antigravity-cli/antigravity-oauth-token` on POSIX, the `gemini:antigravity` Windows Credential Manager entry on Windows (agy writes no token file there). See [`docs/collectors/antigravity.md`](collectors/antigravity.md#token-lifetime--keep-alive) for the systemd-timer alternative and the pre-expiry warning behavior.

**xAI (Grok).** The access token lives about six hours. The server never refreshes a login a sidecar pushed (xAI rotates the refresh token on every refresh — verified against `auth.x.ai` — so a server-side refresh would sign the CLI out), so with `--keep-alive` the sidecar renews it itself: shortly before expiry it refreshes the token at `auth.x.ai` and **writes the result back** to OpenCode's `auth.json` (`xai` entry) or `~/.grok/auth.json`, touching only the xAI entry, atomically and preserving file mode. If the CLI renewed concurrently, the sidecar's result is dropped. A rejected refresh token (`invalid_grant`) is logged once and not retried with the same token — log in again with the CLI and the next renewal pass picks up the new refresh token (no sidecar restart needed). **Turn it on from the dashboard (no config edit).** On the Fleet page each sidecar card has a **Settings** button; its dialog has a **Keep logins alive** switch (the card itself shows the status); Settings → Credentials shows a `keep-alive: off` note with a **Turn on** button next to any agy or xAI login that a sidecar owns, and the Home "Collection failing" banner links to Fleet when keep-alive is the fix. The server returns `keep_alive_desired` on every check-in and the sidecar starts or stops its keep-alive thread without a restart. The status line shows *Requested on/off — applies on next check-in* until the sidecar confirms, *waiting for the sidecar to confirm* for a sidecar that never reports it (the tray app and older builds don't support keep-alive), and notes when the sidecar is offline (it applies when it reconnects). The override is not written to the sidecar's config, so a config rewrite (e.g. by Ansible) can't undo it; **Use the sidecar's own setting** (API `enabled: null`) hands control back to `--keep-alive` / `"keep_alive"` (and the fleet default below). **Fleet-wide default.** Settings → System has a **Keep logins alive on all sidecars** switch (`sidecar_keep_alive_default`, delivered as `keep_alive_fleet_default` on every check-in): sidecars without a per-sidecar override of their own then run keep-alive. Precedence is *per-sidecar Fleet override* > *local flag **or** fleet default* — the default can only turn keep-alive on, so it never switches off a sidecar started with `--keep-alive`, and an explicit **off** in Fleet still wins. Turning the default off again stops only the sidecars that had it as their sole reason to run. The tray app and older sidecars don't support keep-alive and ignore it. **Per login.** Under the main switch the same dialog has a **Per login** list: every login (Antigravity, Claude Code, Codex, xAI) can follow the sidecar, or be forced **Always on** / **Always off** — e.g. keep the Grok login alive without prodding agy. A per-login override sits on top of the sidecar-level setting (and the fleet default), so precedence is *per-login override* > *per-sidecar override* > *local flag or fleet default*. The server sends the overrides as `keep_alive_desired_providers` on every check-in and the sidecar reports what it actually runs per login (`keep_alive_providers`); the thread runs while any login is on and skips the ones that are off. Settings → Credentials' **Turn on** button now enables just that login. Sidecars that predate this (and the tray app) don't report per-login state, so the dialog says so and offers **Update it** instead of the selects. Each sidecar reports whether keep-alive is on, and Settings → Credentials shows a `keep-alive: on / off / unknown` note on machine-renewed agy and xAI logins (unknown = a sidecar too old to report it, or the tray app, which does not run keep-alive). Without `--keep-alive` the card goes stale after about an hour past expiry, and the stale card says why and points here.

**Claude Code.** The access token lives eight hours and Anthropic rotates the refresh token on **every** refresh; each one is strictly single-use (a rotated-away token answers `400 invalid_grant` immediately, though the newest keeps working). The server never refreshes a login a sidecar pushed, so with `--keep-alive` the sidecar renews it itself: about 15 minutes before expiry (or once it has lapsed) it refreshes the token at `platform.claude.com` with Claude Code's own request — a JSON body (with the login's own `scope` when its file has one) and no extra headers; the form-encoded body with extra `User-Agent`/`anthropic-beta` headers was answered with HTTP 429 — and **writes the result back** into `~/.claude/.credentials.json` (and every `CLAUDE_CONFIG_DIR` / `claude_config_dirs` login it finds). Only `claudeAiOauth` changes (`accessToken`, `refreshToken`, `expiresAt`, `refreshTokenExpiresAt`); the sibling `mcpOAuth` block and every other key survive, and the file keeps its mode. A running Claude Code session re-reads the file, so nothing needs restarting. Because tokens are single-use, a rejection means Claude Code refreshed first (the file's refresh token changed) and is not treated as a logout; only an unchanged file with a rejected token is logged once, with advice to run `claude auth login`. A file written in the last 30 seconds is left alone. **macOS keychain logins are not renewed** (Claude Code keeps them in the keychain there; see #526).

**Codex (ChatGPT).** The access token is a JWT that lives ten days and Codex renews it only when it next runs (about nine days after the last refresh), so a CLI idle for longer is left logged out. The server never refreshes a login a sidecar pushed (the refresh token rotates), so with `--keep-alive` the sidecar renews it a day before expiry: it refreshes the token at `auth.openai.com` with Codex's own request (a form body of `grant_type`, `refresh_token` and `client_id`, no `scope`) and **writes the result back** into `~/.codex/auth.json` (and every `CODEX_HOME` / `codex_home` login it finds). Only `tokens.access_token`, `refresh_token`, `id_token` and `last_refresh` change; `auth_mode`, `OPENAI_API_KEY`, `account_id` and every other key survive, and the file keeps its mode. Unlike Claude Code's, a rotated-away Codex refresh token keeps working for a while, but the renewer still treats a rejection as "Codex refreshed first" when the file's refresh token changed, and otherwise logs once with advice to run `codex login`. A file written in the last 30 seconds is left alone.

**opencode v2 logins are renewed in `opencode.db`.** v2 keeps the xAI login in its own database and refreshes it only inside a model call (when the token is within 5 minutes of expiry) — there is no background timer, so an idle host would lapse after ~6h. With keep-alive on, the sidecar refreshes the `credential` row ahead of expiry and writes it back with a compare-and-swap (the row is updated only if its refresh token is still the one exchanged; if opencode renewed first, the sidecar's result is dropped). opencode re-reads the row on every use, so it picks the renewed login up without a restart. The stale `auth.json` v2 leaves behind is never refreshed or written. If xAI rejects the row's refresh token but that stale file holds a newer one (the old keep-alive kept rotating the file across the v2 upgrade), the sidecar adopts that lineage into the database once. Grok CLI's `~/.grok/auth.json` is unaffected.

**Verified on a live host.** On a production sidecar (systemd, OpenCode's `auth.json`), enabling `"keep_alive": true` renewed both logins on startup: the xAI entry was rewritten with a new access *and* refresh token (every other provider entry, unknown field and the 0600 mode untouched), `agy models` renewed the Antigravity token, `opencode run -m xai/grok-4.3` accepted the renewed login, and the quota cards went fresh within a couple of minutes. Each xAI renewal changes the refresh token, so the sidecar fingerprints a stable claim of the access JWT (`principal_id`/`sub`) for the credential's origin instead, and a renewal keeps the same origin (#523). A sidecar that predates this reported a new origin per renewal; the server retires those superseded source rows when the current origin reports.

**Self-updating a systemd sidecar.** `runway-sidecar-cli --self-update` swaps the binary on disk (keeping `.previous`) and exits; the running daemon keeps the old code until restarted (`systemctl restart runway-sidecar`, or let the daily check / Fleet "Update now" re-exec it).

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
| **Claude** | OAuth / cookie / file | `CLAUDE_CODE_OAUTH_TOKEN`, `~/.claude/.credentials.json` (identity in `~/.claude.json`), `~/.config/claude/oauth_creds.json`, macOS keychain, `sessionKey` cookie, or `~/.claude/statusline.json` |
| **GitHub Copilot** | API token, OAuth | `GITHUB_TOKEN` (from .env or OAuth flow), `gh` CLI (`~/.config/gh/hosts.yml`, or the keyring via `gh auth token`), editor Copilot sign-ins (`apps.json` / `hosts.json` under `~/.config/github-copilot`), the Copilot CLI's plaintext token file (`~/.copilot/config.json`, headless Linux), or Windows Credential Manager |
| **Gemini** | OAuth | `~/.gemini/oauth_creds.json` |
| **ChatGPT** | OAuth / cookie | `CHATGPT_OAUTH_TOKEN`, `~/.codex/auth.json`, or Chrome cookie |
| **OpenCode** | API key / browser cookie / local events | `OPENCODE_API_KEY`, `~/.local/share/opencode/auth.json`, Chrome cookie, or SQLite event database |
| **zAI API/Plan** | API key | `ZAI_API_KEY` |
| **Kimi API** | API key | `KIMI_API_KEY` |
| **Kimi Coding** | API key / CLI credential / cookie | `KIMI_CODE_API_KEY`, `~/.kimi-code/credentials/kimi-code*.json` (access token + expiry only, never the refresh token), or `KIMI_AUTH_TOKEN` / Chrome cookie |
| **OpenRouter** | API key | `OPENROUTER_API_KEY` |
| **MiniMax** | API key | `MINIMAX_API_KEY` |
| **Ollama** | Session cookie | `OLLAMA_SESSION_TOKEN` or browser cookie |
| **Kimi K2** | API key | `KIMI_K2_API_KEY` |
| **Antigravity** | OAuth token (server-side API collector) | sidecar scrapes `~/.gemini/antigravity-cli/antigravity-oauth-token` (or the `gemini:antigravity` Windows Credential Manager entry, where agy keeps no token file) and ships it to the server; server queries the Code Assist cloud API for quota. Sidecar emits only token/cost enrichment (never quota). |

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

### Browser cookie stores

Each collection cycle classifies every browser cookie store once, then reads
from the ones it can. A store it cannot use is reported once per cycle,
naming the store and the reason:

```
WARNING Cookie store unavailable: Chrome/Default/Network/Cookies — held open by
        another process; closing the browser releases the lock but not the
        encryption on Chrome 127+, so it does not make these cookies readable.
        set OLLAMA_SESSION_TOKEN (or paste the cookie in Settings → Providers).
INFO    Cookie stores: 1 of 3 unusable this cycle (locked)
```

The traceback for a store that failed to open is at debug level only, so a
browser that keeps a store locked cannot flood the log. See
[troubleshooting.md](troubleshooting.md) for what each reason means and what
to do about it.

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
trusted CA. Frozen builds bundle [`certifi`](https://pypi.org/project/certifi/),
so a valid public (e.g. Let's Encrypt) cert verifies out of the box. If you hit
this:

- **Frozen macOS updates / Sigstore TUF refresh** — the app selects its bundled
  CA file at startup for OpenSSL clients, including TUF's urllib3 fetcher. An
  explicit `SSL_CERT_FILE` is preserved, including an invalid path that causes
  verification to fail. Older builds affected by [#642](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/642)
  may need a manually downloaded and independently verified fixed app before
  normal updates work. A successful diagnostic with a CA override is not proof
  that an older build's default trust is fixed.
- **Self-signed / internal-CA server** — give the sidecar your CA chain:
  - config `~/.config/runway/sidecar/config.json`: `"ca_bundle": "/path/to/ca.pem"`
  - or env: `RUNWAY_CA_BUNDLE=/path/to/ca.pem` (`SSL_CERT_FILE` also honoured)
- **Last resort (trusted network only)** — disable verification entirely:
  config `"tls_insecure": true` or env `RUNWAY_INSECURE=1`. HMAC still
  authenticates payloads, but the channel is no longer confidential — prefer a
  real CA bundle.

The ingest-specific `ca_bundle` / `RUNWAY_CA_BUNDLE` and insecure settings do not
configure Sigstore's TUF refresh or its signature policy. Update signatures still
require the exact signing identity, issuer, chain and transparency evidence;
trust-refresh failures refuse installation. `SSL_CERT_FILE` is an explicit
process-wide trust override, not an ingest-only setting.

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

### Update resource budgets

Portable update archives are limited to **256 MiB**, signature bundles to **1 MiB**,
checksum responses to **4 KiB**, and release metadata to **4 MiB**. Current signed
edge payloads measured on 2026-10-10 are Linux CLI 52,153,543 bytes, Linux tray
59,861,976 bytes, Windows 38,049,361 bytes, and macOS 92,751,463 bytes. Archives
therefore have nearly three times the largest current package's size in headroom.

Actual bytes are counted even if Content-Length is absent or incorrect. Overflow
is a permanent failure: the response closes, temporary bytes are removed, and the
installed copy/rollback slot remain unchanged. There is no operator override.
Local archives are rejected before hashing when oversized. Release packaging
checks the same archive/bundle budgets so legitimate packages cannot silently
outgrow the updater. A future budget increase must update the documented limits
and ship through a reviewed change. Signature verification remains mandatory.

Release signing uses Cosign 3 modern bundles. Before publishing, CI verifies every portable archive with the same Python verifier and exact main-branch identity used by the updater. Older legacy bundles fail closed; a checksum cannot substitute for a valid signature.

### Extra login directories from Fleet

Fleet → a machine's Settings → **Login directories** accepts Claude Code and Codex
state directories, one per line. These are additions to standard locations,
`CLAUDE_CONFIG_DIR` / `CODEX_HOME`, and the sidecar's local configuration. Paths
refer to the **sidecar machine**, and `~` belongs to its service user. Enter a
directory, not `.credentials.json` or `auth.json`. Missing directories can be
configured before a login is created. Windows drive/UNC paths are supported.

A successful check-in applies the additions without restarting and schedules
collection, unless paused. The status stays pending until the machine reports
its acknowledgement; offline machines apply changes when they reconnect. Older
builds must update before these controls become available. Clearing Fleet
additions preserves local/environment discovery. Additions survive connection
failures in memory and are retrieved again after a restart; they are not written
to local config.json and are cleared when the pairing/server changes.

Credentials → **By machine** → **Login directories searched** lists the directory
candidates from the last check-in, even when the machine reports no credentials.
The list includes missing locations; it is not proof that a credential was found.
The sidecar probes known credential/identity files, `projects/` and `sessions/`.
The server never expands or opens these paths. Tray collection uses the same
settings; tray keep-alive remains unsupported (#548), and custom macOS keychain
renewal remains tracked separately (#526).

### macOS Claude Code Keychain renewal (#526)

Claude Code Keychain discovery remains read-only by default. To allow this
machine to renew those logins, add **`"claude_keychain_renewal": true`** to its
local sidecar `config.json` and enable Claude keep-alive using the existing
keep-alive controls. Only the literal JSON boolean `true` grants permission;
a fleet default or dashboard override cannot grant it. Removing the setting or
setting it to `false` prevents further exchanges and writes. A daemon observes
its current local configuration; the CLI daemon requires a restart for a
file edit, while desktop configuration reload replaces the current config.
The tray app currently does not arm keep-alive, so this permission alone does
not start renewal there.

The sidecar checks the default and configured/suffixed Claude Code services,
scoped to the same local account as discovery. It updates the exact existing
Keychain item, preserving its account, service, access controls, and unrelated
JSON fields. Native IO runs in a private helper with a 30-second deadline per
operation. macOS may ask for Keychain permission. A denial or timeout suppresses
further Claude Code discovery and renewal access until sidecar restart; it is
never retried automatically during that process. No refresh token goes to the
Runway server. Claude Desktop OAuth is a separate source and is not renewed.

Before exchanging a single-use refresh token, Runway creates and verifies an
intent record under the Keychain service **Runway Claude Code renewal**, keyed
by the exact target's persistent reference. A successful response is saved there
before write-back. After read-back verifies the update, the recovery record is
removed. A restart retries a pending **save**, never a second exchange. Failed
journal writes retain the replacement in memory for a save retry while the
process lives. If a request times out, or the process crashes after the provider
rotates the token but before the response is durably saved, the intent remains
ambiguous: automatic reuse is blocked until Claude Code renews or the user logs
in again. Pending records remain in Keychain when permission is disabled; they
are processed when permission and Claude keep-alive are enabled again. Deleted
targets are never recreated. Recovery data for a deleted or no-longer-configured
target is retained and is not exported to the server.

Runway serializes its own file and Keychain renewers across threads and processes
with a per-user lock. It re-reads before exchange and write-back and preserves a
login changed by Claude Code. Keychain has no atomic compare-and-swap against
Claude Code's refresh token, so a narrow race with an independently refreshing
Claude Code process remains. A locked/inaccessible Keychain, unsupported JSON,
missing expiry, or ambiguous item match fails closed with a sanitized diagnostic.

Native acceptance must use a dedicated macOS test user, an isolated Keychain,
and a disposable Claude Code login, with explicit approval before credential
access or rotation. Record OS/architecture, build revision, service selection
(default/suffixed, without identifiers), consent, and sanitized operation outcomes.
Verify actual refresh and expiry advancement, preserved ACLs and JSON, acceptance
by Claude Code, denial/timeout suppression, restart recovery, and the packaged
sidecar. Synthetic tests and a framework serialization check alone do not prove
native credential compatibility. Keep #526 open until this acceptance passes.

The remaining native renewal acceptance is tracked in
[#750](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/750).
