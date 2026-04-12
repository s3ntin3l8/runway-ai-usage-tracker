# Troubleshooting

## Chrome Cookie Decryption Fails (App-Bound Encryption)

Chrome 127+ and Edge 127+ enforce **App-Bound Encryption (ABE)**, which ties the cookie
decryption key cryptographically to the browser's own app bundle. This means external
tools — including Runway — can no longer decrypt Chrome/Edge cookies using the standard
macOS Keychain or Windows DPAPI mechanism.

**How to tell if ABE is the cause:**
```bash
source .venv/bin/activate
python3 scripts/debug_chrome_cookies.py
```
Look for `⚠️  No 'os_crypt' in Local State` or `Cause: ABE suspected`. The script will
also print platform-specific workarounds automatically.

---

### ✅ Recommended Workarounds

#### macOS — Use Safari (easiest)

Safari cookies are stored in a binary format with **no encryption** — Runway reads them
directly. Simply log into the provider in Safari and Runway will pick up the session
automatically. No configuration needed.

Supported providers with Safari fallback: Claude, OpenCode, ChatGPT, Kimi, Ollama.

#### Windows / Linux — Use Firefox (easiest)

Firefox stores cookies in an **unencrypted SQLite database**, which Runway can read
directly. Log into the provider in Firefox and Runway will use that session.

> **Note**: Edge is subject to the same ABE restrictions as Chrome (identical Chromium
> codebase). Firefox is the recommended alternative on Windows.

---

### ⚙️ Alternative: Use Environment Variables

For a config-first approach, set the session token directly. This also works in Docker.

| Provider | Environment Variable |
|----------|---------------------|
| Claude (OAuth) | `CLAUDE_CODE_OAUTH_TOKEN` |
| ChatGPT | `CHATGPT_OAUTH_TOKEN` |
| Ollama | `OLLAMA_SESSION_TOKEN` |
| Kimi | `KIMI_AUTH_TOKEN` |

To find your token: open DevTools in any browser → Application → Cookies → copy the
relevant cookie value for the provider's domain.

---

### 🔧 Dev-Only: Disable ABE Temporarily

> ⚠️ **Security risk.** Only do this in a trusted local dev environment. Re-enable
> immediately after extracting cookies. Fully quit and relaunch the browser after
> any change.

#### macOS

No admin rights required. Uses macOS user preferences (`defaults`).

**Chrome:**
```bash
# Disable (then fully quit and relaunch Chrome)
defaults write com.google.Chrome ApplicationBoundEncryptionEnabled -bool false

# Re-enable when done
defaults delete com.google.Chrome ApplicationBoundEncryptionEnabled
```

**Edge:**
```bash
# Disable
defaults write com.microsoft.Edge ApplicationBoundEncryptionEnabled -bool false

# Re-enable
defaults delete com.microsoft.Edge ApplicationBoundEncryptionEnabled
```

#### Windows

> ⚠️ Requires an **elevated (Administrator) command prompt**.

On Windows, ABE is enforced by the Chrome Elevation Service (`elevation_service.exe`,
runs as SYSTEM). Disabling it requires writing a Group Policy key to `HKLM`, which
needs admin rights.

**Chrome:**
```bat
:: Disable
reg add "HKLM\SOFTWARE\Policies\Google\Chrome" /v ApplicationBoundEncryptionEnabled /t REG_DWORD /d 0 /f

:: Re-enable when done
reg delete "HKLM\SOFTWARE\Policies\Google\Chrome" /v ApplicationBoundEncryptionEnabled /f
```

**Edge:**
```bat
:: Disable
reg add "HKLM\SOFTWARE\Policies\Microsoft\Edge" /v ApplicationBoundEncryptionEnabled /t REG_DWORD /d 0 /f

:: Re-enable when done
reg delete "HKLM\SOFTWARE\Policies\Microsoft\Edge" /v ApplicationBoundEncryptionEnabled /f
```

After relaunching the browser with ABE disabled, run `debug_chrome_cookies.py` to
confirm decryption succeeds, extract your session token into an environment variable,
then re-enable ABE.
