# GitHub Copilot

**File:** `app/services/collectors/github.py`

GitHub Copilot quota. One endpoint returns the plan and either quota snapshots (paid plans) or limited/monthly quotas (free plan).

## At a glance

- **Strategies:** `api` only (`copilot_internal/user`); there is no fallback endpoint. Results are cached for 5 minutes.
- **Cards:** one per quota the plan reports, variant `Copilot` service: `Completions` and `Chat` (free plan, from `limited_user_quotas` against `monthly_quotas`), or `Premium Interactions`, `Chat`, `Completions`, `Included Credits` from `quota_snapshots` (metrics with `has_quota: false` are skipped). Snapshot cards are `window_type: monthly`, `unit_type: requests`. A free-plan quota without a known total shows no percentage rather than inventing one.
- **Labels:** `data_source=api`; `input_source` is `config`, `server` or `sidecar` by where the token came from. See [Strategy Types](../collection_logic.md#strategy-types).
- **Account identity:** an account is keyed by its GitHub login (`/user` returns `login` for every token type and it never changes), never by email, which is only the display label. The server host's `git config user.email` and `gh` login are not consulted: they identify the host, not the token.

## Credentials

The collector uses the first one the server finds: Settings key, then env, then Runway's own `github_oauth.json`. Every machine-reported credential is kept as its own row in Settings, Credentials.

| Source | Detected by | Notes |
|---|---|---|
| Settings key | server | A token pasted in the provider dialog. |
| `GITHUB_TOKEN` / `GH_TOKEN` | server and sidecar | Environment variable; both set to one value count once. |
| Sign in with GitHub (device flow) | server | Button in the GitHub account dialog. The token is stored encrypted at `<config dir>/github_oauth.json` (`GITHUB_OAUTH_PATH`), counts as evidence for the account while the file exists, and Disconnect deletes it. Sidecars never ship this file. |
| `gh` CLI `hosts.yml` | sidecar | `~/.config/gh/hosts.yml` (Linux/macOS) or `%APPDATA%\GitHub CLI\hosts.yml`. |
| `gh` CLI keyring | sidecar | `gh auth token`, for `gh` 2.40 and later, which keeps the token in the OS keyring. Skipped when it returns a token already found. |
| Editor Copilot sign-in files | sidecar | `apps.json` / `hosts.json` under `~/.config/github-copilot` (or the platform data dir), written by JetBrains, Neovim, Xcode and Zed. `github.com` entries only; the known client ids are tried first. The same token in several files counts once. |
| Copilot CLI plaintext token | sidecar | `~/.copilot/config.json` (`authTokens` / `copilotTokens` for `https://github.com:<login>`). Holds a token only on a machine without an OS keychain. `COPILOT_HOME` is not honoured. |
| Windows Credential Manager | sidecar | Target `git:https://github.com`. |

Not detected: the VS Code Copilot extension (encrypted secret storage), the Copilot CLI keychain entry, GitHub Enterprise Cloud (`*.ghe.com`) sign-ins, browser cookies.

An OAuth token (from `gh auth login` or the device login) works best; a classic PAT may be rejected by the Copilot endpoints. The sidecar only forwards credentials and never calls the GitHub API; the server verifies the account with `/user`.

**Own OAuth app.** The device login uses the bundled public client id (`GITHUB_CLIENT_ID`, default `Iv1.b507a08c87ecfe98`). To use your own, create an OAuth App at GitHub Developer Settings, tick **Enable Device Flow** (without it the login returns 404), set `GITHUB_CLIENT_ID` in `.env`, and restart.

## Endpoints

| Endpoint | Notes |
|---|---|
| `GET api.github.com/copilot_internal/user` | Plan, `quota_snapshots`, and the free-plan `limited_user_quotas` / `monthly_quotas`. Sent with VS Code Copilot extension headers and `Authorization: token <token>`. |
| `GET api.github.com/user` | Login and identity. |
| `GET api.github.com/user/emails` | Display label. |

`copilot_internal/v2/token` is deliberately not called: it only mints a short-lived model-proxy token and carries no totals.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No Copilot data | Check the token is set and belongs to an account with an active Copilot subscription (a classic PAT may be rejected). |
| 401/403 | Regenerate the token, or prefer `gh auth login` or Sign in with GitHub over a classic PAT; verify the subscription at github.com/settings/copilot. |
| Device login returns 404 | The OAuth App lacks Enable Device Flow. |
| Credential missing on a machine | The sidecar log (`~/.config/runway/sidecar/sidecar.log`) says `file read but no credential keys found` once per file when a rule's file exists but holds no token (a logged-out `gh`, or a token moved to the keyring). |
| `make dev` shows no `gh` login | The server reads no credential files; use `make dev-all`. |

`RUNWAY_CONFIG_DIR` overrides the config directory (where `github_oauth.json` lives), useful for Docker or multi-host setups.

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/github.py` | Collector: the Copilot internal API |
| `app/services/collectors/base.py` | Collector base class |
| `scripts/sidecar.py` | Credential rules (env, `hosts.yml`, keyring, editor files, Copilot CLI) |
| `app/core/registry.json` | `github` provider rules shared with the sidecar |
