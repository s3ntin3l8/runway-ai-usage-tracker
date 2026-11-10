# Troubleshooting

Find the symptom, then the fix. Sidecar setup lives in [sidecar.md](sidecar.md).
The sidecar log is `~/.config/runway/sidecar/sidecar.log` (Windows:
`%APPDATA%\runway\sidecar\sidecar.log`); the tray's **View Logs** opens it, and a
Fleet card's **Logs** shows it remotely. To get more detail, raise the log level
from the card's Settings dialog or set `"log_level": "DEBUG"` in `config.json`
(there is no `--verbose` flag).

## The sidecar is not collecting

Run one cycle and read the output:

```bash
python3 scripts/sidecar.py --run-once       # or runway-sidecar-cli --run-once
```

There is no per-provider selector flag. Enable or disable providers in the
dashboard's Fleet settings. If a provider is on but empty, check that its login
exists on the sidecar's machine and under the user the sidecar runs as: a
service does not see your shell environment (use the `claude_config_dirs` /
`codex_home` config keys or Fleet's **Login directories**).

## Pushes fail

- Reach the server from the sidecar machine: `curl http://server:8765/api/v1/system/health`.
- Check `api_url` and `api_key` in `config.json`, or re-pair ([sidecar.md](sidecar.md#pair-with-a-server)).
- A 400 about the timestamp means the machine's clock is more than five minutes behind (or one minute ahead of) the server.
- Look in `queue/` beside the config: queued files mean the server was unreachable and will be replayed.
- Pairing refused, or a paired Mac keeps its Keychain credentials back, behind a forward-auth proxy: [Forward-Auth](forward-auth.md#verify-the-bypass).

## `CERTIFICATE_VERIFY_FAILED`

The sidecar could not verify the server's TLS certificate. Give it your CA
chain (`ca_bundle` or `RUNWAY_CA_BUNDLE`), or as a last resort on a trusted
network disable verification. See
[sidecar.md](sidecar.md#tls--certificate-errors-certificate_verify_failed) and
[deployment.md](deployment.md).

## The daemon will not start

The PID file `sidecar.pid` beside the config prevents a second daemon. If the
previous one crashed, make sure no sidecar process is running, then delete the
PID file and start again.

## A self-update fails

The checksum and Sigstore signature are mandatory; a mismatch, a missing
bundle or a trust or network failure leaves the installed copy unchanged. If
the install directory is not writable the update is skipped (install the DMG
or setup.exe manually). `--verify-update ARCHIVE --bundle BUNDLE` checks an
archive without installing it, and `--rollback` restores the previous build.

## A provider has no data and the browser cookie cannot be read

The sidecar classifies every browser cookie store once per cycle and reports a
store it cannot use once, naming the store and the reason:

```
WARNING Cookie store unavailable: Chrome/Default/Network/Cookies — held open by another
        process; closing the browser releases the lock but not the encryption on Chrome
        127+, so it does not make these cookies readable. set OLLAMA_SESSION_TOKEN (or
        paste the cookie in Settings → Providers).
INFO    Cookie stores: 1 of 3 unusable this cycle (locked)
```

The dashboard shows the same thing: a "Cookie store unavailable" banner on
Home, Settings → Credentials → **Unavailable** with the store, reason and fix,
and the provider page says why instead of "No data for this provider". It
clears itself the cycle the store reads fine again.

A working cookie path logs `[<provider>] cookie '<name>' found on <domain>` at
INFO. If that line is missing for a provider you are signed in to in
Chrome or Edge 127+, the reason in the warning tells you which case applies:

| Reason | Meaning | Fix |
|---|---|---|
| `held open by another process` | The browser holds the file exclusively (Windows). | Closing the browser is not enough on Chrome 127+, because values are also app-bound encrypted. Use Firefox or one of the alternatives below. |
| `values are app-bound encrypted (v20)` | The store opened, but the values cannot be decrypted. | Use Firefox or one of the alternatives below. |

### Why: App-Bound Encryption

Chrome and Edge 127+ tie cookie decryption to the browser's own app bundle, so
external tools cannot decrypt the cookies with the macOS Keychain or Windows
DPAPI. Cross-check by signing in to the same provider in Safari (macOS) or
Firefox and re-running a cycle: if the cookie is then found, encryption was the
blocker.

### What works

- **macOS: Safari.** Its cookie store is unencrypted; sign in there. Safari fallback covers Claude, OpenCode, ChatGPT, Kimi and Ollama.
- **Windows and Linux: Firefox.** Its cookie database is unencrypted and stays readable while the browser runs; no restart or policy change. Edge has the same restriction as Chrome.
- **Environment variables or the Settings paste.** These work in any browser and in Docker. Copy the cookie value from DevTools → Application → Cookies for the provider's domain.

| Provider | Variable |
|---|---|
| Claude (OAuth) | `CLAUDE_CODE_OAUTH_TOKEN` |
| ChatGPT | `CHATGPT_OAUTH_TOKEN` |
| Ollama | `OLLAMA_SESSION_TOKEN` |
| Kimi Coding | `KIMI_CODE_API_KEY` (preferred) or `KIMI_AUTH_TOKEN` |

### Dev only: disabling ABE on Windows

This lowers browser security: only do it on a trusted dev machine, from an
elevated prompt, and revert as soon as you have extracted the token. Fully
quit and relaunch the browser after each change. There is no equivalent on
macOS (the policy is Windows-only).

```bat
:: Chrome: disable, then re-enable
reg add "HKLM\SOFTWARE\Policies\Google\Chrome" /v ApplicationBoundEncryptionEnabled /t REG_DWORD /d 0 /f
reg delete "HKLM\SOFTWARE\Policies\Google\Chrome" /v ApplicationBoundEncryptionEnabled /f

:: Edge: disable, then re-enable
reg add "HKLM\SOFTWARE\Policies\Microsoft\Edge" /v ApplicationBoundEncryptionEnabled /t REG_DWORD /d 0 /f
reg delete "HKLM\SOFTWARE\Policies\Microsoft\Edge" /v ApplicationBoundEncryptionEnabled /f
```

Run a cycle, confirm the `cookie ... found` line, store the token in an
environment variable, then re-enable ABE.

## A login on a CLI-managed machine goes stale

Antigravity, xAI, Claude Code, Codex and MiniMax Code logins are renewed only by their own
CLI. On a host where the CLI rarely runs the card goes stale about an hour
past expiry (the MiniMax Code token lasts twelve hours). Turn on keep-alive:
[sidecar.md](sidecar.md#keep-alive-for-antigravity-claude-code-codex-minimax-code-and-xai-opt-in).
The tray app does not support it; use the headless CLI.

## Dashboard cards or account attribution look wrong

Check Settings → Data health first. A stale `default`-keyed config, an event
stuck under a legacy provider id or an orphaned gauge series are detected and
fixed there. See [data-health.md](data-health.md).
