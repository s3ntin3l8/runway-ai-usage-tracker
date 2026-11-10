# Sidecar

The sidecar runs on a workstation, reads local credentials and usage (CLI
logins, browser cookies, IDE and log files) and pushes them to the Runway
server. The server never does local detection itself. The wire protocol is
described in [`fleet-protocol.md`](fleet-protocol.md); packaging and signing in
[`sidecar-build.md`](sidecar-build.md).

The sidecar ships three ways:

- a **desktop tray app** for macOS, Windows and Linux,
- a **headless CLI binary** (`runway-sidecar-cli`) for Linux servers, Docker and CI,
- the Python source, `scripts/sidecar.py` (`make sidecar` from a checkout).

All three take the same flags; substitute the entry point you use. The source
run needs the repository checkout (it imports `app.core.*`) and the `sidecar`
extra for update verification (`pip install '.[sidecar]'`); the packaged
builds bundle everything.

## Install

Download from the [Releases page](https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/latest)
(the dashboard's **Fleet** page also links the right installer). `<version>` is the release tag.

| Platform | Asset | Install |
|---|---|---|
| macOS (Apple Silicon) | `Runway-Sidecar-macOS-<version>.dmg` | Drag **Runway Sidecar** onto **Applications** and launch it from there. |
| Windows | `Runway-Sidecar-Windows-<version>-setup.exe` | Per-user install into `%LOCALAPPDATA%\Programs\Runway Sidecar` (no admin prompt); the last page offers **Start automatically when I sign in**. |
| Linux (tray) | `Runway-Sidecar-Linux-<version>.tar.gz` | Extract and run `./RunwaySidecar`. Needs a tray host (AppIndicator or GTK) and a DBus session. |
| Linux (headless) | `Runway-Sidecar-Linux-CLI-<version>.tar.gz` | Extract and run `./runway-sidecar-cli --daemon`. Single file, no Python needed. |

The release also carries portable `.zip` builds for macOS and Windows; the
self-updater downloads those, you normally do not need them.

**Windows silent install:** `.\Runway-Sidecar-Windows-<version>-setup.exe /S /AUTOSTART=1`
(`/D=C:\path` overrides the install directory and must come last).

**Uninstall.** macOS: quit the app, turn off **Launch at Login**, move the app to the Trash.
Windows: *Settings → Apps → Runway Sidecar → Uninstall*, or silently
`"%LOCALAPPDATA%\Programs\Runway Sidecar\uninstall.exe" /S`. Neither removes
your config, offline queue or logs (`~/.config/runway/sidecar`,
`%APPDATA%\runway\sidecar`); delete that folder for a clean slate.

### Unsigned app warning

The sidecar has no paid Apple Developer ID or Windows code-signing certificate, so each OS asks once.

- **macOS (Gatekeeper):** the app is ad-hoc signed, not notarized. Right-click it in Applications, choose **Open**, then **Open** again (macOS 15+: try once, then *System Settings → Privacy & Security → Open Anyway*). Launch from **Applications**, not from the DMG window: a copy running off the disk image cannot update itself or register Launch at Login.
- **Windows (SmartScreen):** on *Windows protected your PC*, click **More info** then **Run anyway**.

### Verify a download

Each asset has a `.sha256` file and each release has `SHA256SUMS.txt`:

```bash
sha256sum -c --ignore-missing SHA256SUMS.txt      # macOS: shasum -a 256 -c …
```

Assets are also signed with [Sigstore](https://www.sigstore.dev/) keyless signing (`<asset>.sigstore.json`):

```bash
cosign verify-blob \
  --bundle Runway-Sidecar-macOS-<version>.dmg.sigstore.json \
  --certificate-identity https://github.com/s3ntin3l8/runway-ai-usage-tracker/.github/workflows/sidecar-build.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  Runway-Sidecar-macOS-<version>.dmg
```

### Edge and beta builds

- **Edge** is a rolling build from `main`, published to a single always-overwritten `edge` prerelease with stable URLs such as `https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/edge/Runway-Sidecar-Linux-CLI-edge.tar.gz` (also `…-macOS-edge.dmg`, `…-Windows-edge-setup.exe`, `…-Linux-edge.tar.gz`). It reports its version as `<base>+edge.<short-sha>`. It is a prerelease, so stable sidecars and the dashboard's "update available" flag ignore it.
- **Beta** releases use tags like `v3.0.0-beta.1`; the Fleet download card's **Beta** option picks the newest.

To make installed sidecars follow either, set *System → Sidecar update channel* (fleet) or a per-sidecar channel (see [Updates](#updates)).

## Pair with a server

**From the dashboard (easiest).** *Fleet → Add sidecar → Generate pairing link*, then **Open in Runway Sidecar**. The sidecar shows a confirmation page naming your server; check the address and click **Pair**. It fetches the server URL and keys itself and starts reporting. Codes are one-time and expire after 10 minutes (`PAIRING_CODE_TTL_SECONDS`).

- If the link does not open the app, use the tray icon's **Settings…** then **Pair with a code…** and enter the address and code.
- Headless: `runway-sidecar-cli --pair https://your-server CODE` (or pass the whole `runway-sidecar://pair?…` link). It writes the config and exits; then start the daemon.
- Only pair with a link generated in **your own** dashboard: pairing points this machine's usage data and sign-ins at that server.
- Behind a forward-auth proxy, `POST /api/v1/fleet/pair` and `POST /api/v1/fleet/source-sync` must reach Runway unauthenticated; see [Forward-Auth](forward-auth.md#verify-the-bypass).

**Manual.** Without a config file the app writes a template and opens it. Set `api_url` (for example `http://localhost:8765`) and `api_key` (the ingest key from the Runway instance), then restart.

## Configuration

Config file: `~/.config/runway/sidecar/config.json` (macOS/Linux) or `%APPDATA%\runway\sidecar\config.json` (Windows). `RUNWAY_CONFIG_DIR` relocates it to `<dir>/sidecar/config.json`; `--config PATH` overrides both.

| Key | Default | Meaning |
|---|---|---|
| `api_url` / `api_key` | required | Server address and ingest key. |
| `retry_attempts` / `retry_backoff_seconds` | `3` / `5` | Push retry with exponential backoff. |
| `queue_max_size_mb` | `10` | Offline queue cap. |
| `log_level` / `log_file_enabled` | `INFO` / `true` | Logging. |
| `auto_update` | absent | `true` always auto-installs, `false` never does, absent defers to the server toggle. |
| `keep_alive` | `false` | Same as `--keep-alive`. |
| `claude_config_dirs`, `codex_home` | none | Extra Claude Code / Codex state directories (list or comma-separated string). |
| `ca_bundle`, `tls_insecure` | none | TLS trust settings, see [below](#tls--certificate-errors-certificate_verify_failed). |
| `claude_keychain_renewal` | absent | macOS only; literal `true` lets keep-alive renew Claude Code logins held in the Keychain (local-only, never settable remotely). |

Polling cadence and the enabled provider list are server-controlled (the ingest response); there is no local `interval_seconds` or `providers` key. Set them in the dashboard's Fleet settings.

### Environment variables

These are host-side, set in the shell, service unit or launch agent (not the server's `.env`).

| Variable | Effect |
|---|---|
| `RUNWAY_CONFIG_DIR` | Config directory override. |
| `RUNWAY_API_URL`, `RUNWAY_API_KEY` | Override `api_url` / `api_key`. |
| `RUNWAY_CA_BUNDLE`, `SSL_CERT_FILE` | CA bundle for TLS verification; `SSL_CERT_FILE` is a process-wide override. |
| `RUNWAY_INSECURE` | `1` disables TLS verification. |
| `RUNWAY_UPDATE_CHANNEL` | `stable`, `beta` or `edge` for this host. |
| `SIDECAR_BOOTSTRAP_DAYS` | How many days of history a provider's first run imports (default `90`; unparsable values fall back to the default). |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME` | Extra Claude Code / Codex state directories, searched in addition to `~/.claude` / `~/.codex`. |
| `OPENCODE_DB` | opencode SQLite event database, when not in the default place. |
| `GROK_HOME` | Grok CLI home, when not `~/.grok`. |
| `HERMES_HOME` | Hermes agent home, when not `~/.hermes` (Hermes is a sidecar-only source). |
| `MINIMAX_CODE_HOME`, `MINIMAX_DATA_DIR` | MiniMax Code runtime locations. |
| `RUNWAY_CLAUDE_DESKTOP_CREDENTIALS` | `0`, `false`, `off` or `no` skips the Claude Desktop cookie store on macOS. |

A service does not inherit your shell environment: for services use the `claude_config_dirs` / `codex_home` config keys, or **Login directories** in the machine's Fleet settings (one directory per line, paths relative to the sidecar machine). Fleet additions apply on the next check-in without a restart and are not written to `config.json`. Credentials → **By machine** → *Login directories searched* shows what the last check-in covered.

Provider credential variables (for example `ZAI_API_KEY`) are listed in [`collectors/README.md`](collectors/README.md) and each collector page.

### Log level from the dashboard

`log_level` in the config is read at startup. A sidecar's **Settings** dialog (Fleet) also has a **Log level** list; the choice reaches the sidecar on its next check-in, applies without a restart, is not written to the config, and stays until you pick *Use sidecar config*. View output with **Logs** on the Fleet card or `~/.config/runway/sidecar/sidecar.log`. There is no `--verbose` or `--log-level` flag.

## Command line

Anything not listed is not a flag; unknown flags exit with status 2.

| Flag | Effect |
|---|---|
| `--config PATH` | Use this config file. |
| `--run-once` | One collection cycle, then exit. |
| `--daemon` | Run continuously (the default when no mode flag is given). |
| `--keep-alive` | Daemon: renew CLI logins that only their own CLI renews, see [Keep-alive](#keep-alive-for-antigravity-claude-code-codex-minimax-code-and-xai-opt-in). |
| `--self-update` (`--update`) | Download, verify and install the latest build, then exit. |
| `--rollback` | Restore the build the last self-update replaced, then exit. |
| `--pair LINK_OR_SERVER [CODE]` | Pair and write `api_url`/`api_key`, then exit. |
| `--version` | Print the version. |
| `--probe-minimax-code` | Read-only report of the MiniMax Code store on this host; exit 0 if readable, 1 otherwise. See [`collectors/minimax_code.md`](collectors/minimax_code.md#checking-a-host). |
| `--verify-update ARCHIVE --bundle BUNDLE` | Verify an update archive against a Sigstore bundle without installing. |
| `--authorize-keychain-source SOURCE`, `--authorize-claude-keychain` | macOS packaged build only: grant one Keychain read, then exit. |

(`--runway-keychain-worker` is the sidecar's own internal channel, not a user mode.)

```bash
python3 scripts/sidecar.py --run-once        # first run creates the config template
python3 scripts/sidecar.py --daemon          # or ./runway-sidecar-cli --daemon
```

A PID file (`sidecar.pid` beside the config) prevents a second daemon. Stop with `kill $(cat ~/.config/runway/sidecar/sidecar.pid)`.

## Running as a service

### systemd (Linux)

A template unit runs one instance per user. Create `/etc/systemd/system/runway-sidecar@.service`:

```ini
[Unit]
Description=Runway Sidecar (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%i
ExecStart=/usr/local/bin/runway-sidecar-cli --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now runway-sidecar@$USER
```

Use `Restart=always` for unattended hosts: after a self-update the CLI exits and lets systemd start the new build, which keeps the process tree clean. Without a supervisor it re-executes in place and leaves an idle bootloader process (about 2 MB) per update.

### LaunchAgent (macOS)

The tray app's **Launch at Login** manages its own login item. To run the CLI instead, create `~/Library/LaunchAgents/com.runway.sidecar.plist` with `ProgramArguments` set to the binary and `--daemon`, `RunAtLoad` and `KeepAlive` true, then `launchctl load` it. Do not use the label `com.runway.sidecar` if the tray app's autostart is also enabled: it writes the same file.

### Windows

The installer's *Start automatically when I sign in* checkbox and the tray's **Launch at Login** are the same setting (`HKCU\…\Run`).

## Tray app

The tray icon shows status: green healthy, amber warning or stale data, red error or config needed, grey paused. The menu has **Open Dashboard**, **Run Now**, **Pause / Resume**, **Launch at Login**, **Edit Config**, **View Logs**, **Check for Updates…**, **View Releases…**, **Download & Install Update** (only when one is available), **Roll Back to vX.Y.Z** (only after a self-update) and **Quit**. The status panel shows **Last sync** (last successful server contact, including idle heartbeats) separately from **Last upload** (the last successful data upload and its metric and event counts).

## Updates

The sidecar checks daily; the menu title then shows **(update available)**. Ways to install:

- **Tray:** **Download & Install Update**.
- **CLI:** `runway-sidecar-cli --self-update`, then restart under your supervisor (`systemctl restart` or let the daily check re-exec it).
- **Automatic:** fleet-wide via *System → Auto-install updates* (default off), per sidecar via Fleet → Settings → **Updates** (*Auto-update* and *Update channel*, each *Follow fleet* or an explicit value), or locally with `auto_update` in `config.json`, which wins over both.
- **Push:** Fleet's **Update now** on a card makes that sidecar self-install on its next heartbeat.

Safety rules:

- Self-update downloads the portable `.zip`/`.tar.gz`, verifies the mandatory `.sha256` and the Sigstore bundle (pinned to this repository's signing workflow and the GitHub issuer), then swaps the install. Any failure leaves the running copy untouched.
- Only packaged builds update themselves. Source runs and Docker are notify-only: `git pull` or repull the image.
- If the install location is not writable (for example `/Applications` for a non-admin macOS user), the update is skipped and logged; install the DMG or setup.exe from **View Releases…**.
- The replaced build is kept next to the install as `<name>.previous`; **Roll Back** or `--rollback` restores it (one backup is kept).
- Remote server URLs must use HTTPS (loopback HTTP is allowed). Signed requests and pairing never follow redirects. See [SECURITY.md](SECURITY.md).

## Keep-alive for Antigravity, Claude Code, Codex, MiniMax Code and xAI (opt-in)

These logins are renewed only by their own CLI, so on a host where that CLI runs rarely the quota card goes stale. With keep-alive the sidecar renews them and writes the new tokens back into the CLI's own credential file, touching only the renewed entry and keeping file mode:

| Login | What the sidecar does |
|---|---|
| Antigravity | When the token has lapsed, runs `agy models`, which renews it in place. |
| xAI (Grok) | Refreshes shortly before expiry and writes `~/.grok/auth.json` or OpenCode's `auth.json` (opencode v2: the `credential` row in `opencode.db`, compare-and-swap). |
| Claude Code | Refreshes about 15 minutes before expiry into `~/.claude/.credentials.json` and every `CLAUDE_CONFIG_DIR` / `claude_config_dirs` login. |
| Codex (ChatGPT) | Refreshes a day before expiry into `~/.codex/auth.json` and every `CODEX_HOME` / `codex_home` login. |
| MiniMax Code | Refreshes 90 minutes before expiry into the CLI's own `~/.minimax/auth/<env>/<region>/mcode-public/auth.json`, and keeps its `auth-state.json` in step (compare-and-swap on the refresh token and the generation). |

The server never refreshes a login a sidecar pushed, because these providers rotate refresh tokens and a server-side refresh would sign the CLI out. If a CLI refreshed first, the sidecar drops its result; a rejected refresh token is logged once, and you fix it by logging in again with the CLI. Claude Code logins held in the macOS Keychain are renewed only with `claude_keychain_renewal` (above). Provider details: [Antigravity](collectors/antigravity.md#token-lifetime--keep-alive), [Claude](collectors/claude.md), [ChatGPT](collectors/chatgpt.md), [MiniMax](collectors/minimax.md), [xAI](collectors/xai.md).

**MiniMax Code.** The MiniMax Code access token lives twelve hours, and only the CLI or the desktop app renews it — when one of them next runs. The sidecar refreshes it at the account service of the login's region (`account.minimax.io` for `en`, `account.minimax.cn` for `cn`) with the CLI's own request, and writes the result back into the app's store. The app keeps that store in two files, `auth.json` and `auth-state.json`, whose `generation` counters must stay equal — the app answers "login error" when they do not — so the sidecar updates only the token, the expiry and the status — never the generation. If the app is refreshing at the same moment, the sidecar stands down (the state says so, or a credential is staged in `refresh/`) and drops its own result when the app got there first. A login kept in the OS keyring instead of a file (`storeKind: "os-keyring"`) is not renewed yet.

**Turn it on**

- CLI or config: `--keep-alive` or `"keep_alive": true`.
- Fleet: a card's **Settings** dialog has a **Keep logins alive** switch and a **Per login** list (follow the sidecar, *Always on* or *Always off* for each login). Settings → Credentials shows the keep-alive state beside machine-renewed logins with a **Turn on** button. The server delivers the choice on the next check-in; no restart or config edit, and a config rewrite cannot undo it. *Use the sidecar's own setting* hands control back.
- Fleet-wide default: *Settings → System → Keep logins alive on all sidecars*. It can only turn keep-alive on; a sidecar started with `--keep-alive` keeps running, and an explicit per-sidecar **off** still wins. Precedence: per-login override, then per-sidecar override, then local flag or fleet default.

The **tray app does not support keep-alive**; the dashboard reports it as unknown there. Use the headless CLI on that host if you need it.

## macOS

Claude Desktop is a separate source from Claude Code. On macOS the sidecar can read the Desktop app's `claude.ai` `sessionKey` cookie (its `Cookies` store plus the `Claude Safe Storage` Keychain item) and forwards it for the server's Claude web quota check; it never refreshes or writes the Desktop login. Cowork session transcripts are read for exact token events. Desktop quota samples are not turned into token events. Set `RUNWAY_CLAUDE_DESKTOP_CREDENTIALS=off` (also `0`, `false`, `no`) in the sidecar's environment to skip the Desktop cookie store; for a launchd-managed sidecar put it in the job's `EnvironmentVariables`, since a shell export does not reach a separately launched tray app. Keychain-backed reads are governed by [source consent](source-consent.md). Details: [`collectors/claude.md`](collectors/claude.md).

## TLS / certificate errors (`CERTIFICATE_VERIFY_FAILED`)

Packaged builds bundle [`certifi`](https://pypi.org/project/certifi/), so a public certificate such as Let's Encrypt verifies out of the box. For a self-signed or internal-CA server:

- give the sidecar your CA chain: `"ca_bundle": "/path/to/ca.pem"` in `config.json`, or `RUNWAY_CA_BUNDLE` (`SSL_CERT_FILE` is also honoured);
- last resort on a trusted network: `"tls_insecure": true` or `RUNWAY_INSECURE=1`. Payloads stay HMAC-signed but the channel is no longer confidential.

These settings cover pushes to the server. They do not change update verification, which always requires the exact signing identity, certificate chain and transparency evidence. `SSL_CERT_FILE` is a process-wide override and an invalid path makes verification fail. Symptoms and other failures: [troubleshooting.md](troubleshooting.md).

## Deployment shapes

- **Local:** server and sidecar on one machine (`make dev-all`, or `make run-all`).
- **Multi-host:** the server runs once; each workstation pairs its own sidecar.
- **Docker server:** the container needs the non-loopback startup gates (`DB_ENCRYPTION_KEY`, `TLS_TERMINATED`, `CORS_ORIGINS`, and `ADMIN_API_KEY` or `TRUSTED_PROXY_IPS`) and a TLS-terminating proxy; follow [deployment.md](deployment.md). Sidecars always run on the workstations, never in the server container.

## What it keeps on disk

Everything lives in the sidecar config directory:

- `queue/YYYY-MM-DD.jsonl`: the offline queue. When the server is unreachable, payloads are stored here (oldest files dropped past `queue_max_size_mb`) and replayed on reconnect.
- `sidecar.log`: the log (not rotated).
- `sidecar.pid`, event watermarks (last-pushed position per provider and account) and, with pairing, the device key in an owner-only config.

What is forwarded and how the server treats it is described in [`fleet-protocol.md`](fleet-protocol.md). Which local sources a sidecar may read is governed by [source consent](source-consent.md).

## Related

- [Troubleshooting](troubleshooting.md): symptoms and fixes.
- [Collector docs](collectors/README.md): per-provider setup.
- [Source consent](source-consent.md), [Fleet protocol](fleet-protocol.md), [Security model](SECURITY.md).
- [Sidecar build and packaging](sidecar-build.md), [Memory](memory.md).
