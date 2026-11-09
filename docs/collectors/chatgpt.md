# ChatGPT

**File:** `app/services/collectors/chatgpt.py`

ChatGPT Codex quota (the 5-hour and weekly Codex windows) from `wham/usage`, plus per-turn usage events the sidecar extracts from Codex session logs.

## At a glance

- **Strategies** (`STRATEGIES` in `chatgpt.py`): `web` only (Web API), run on the server.
- **Cards:** one per window present in `rate_limit`: `primary_window` and `secondary_window`. Each is classified by its own `limit_window_seconds` (not by plan), so a 5h window becomes `session` and a 7d window `weekly`; a window with no duration falls back to `monthly`. Free/Go plans that report one window get one card; Plus/Pro typically get two. Service name `ChatGPT`, variant `Codex`.
- **Labels:** `data_source=api` for a bearer token, `web` for a cookie exchange; `input_source` is `config`, `server` or `sidecar` depending on where the credential came from. See [Card provenance labels](../collection_logic.md#card-provenance-labels).

## Credentials

Resolved in this order: a refreshed in-memory bearer, then the env/Settings credential (or the pinned source bundle), then the cached sidecar bundle, then cookies.

| Source | Where it comes from | Runs in | Notes |
|---|---|---|---|
| `CHATGPT_OAUTH_TOKEN` | Environment variable | server and sidecar | Bearer token. |
| `~/.codex/auth.json` | `codex` login | sidecar | Also `<Codex config dir>/auth.json` and `$CODEX_HOME/auth.json`. Maps `tokens.access_token`, `refresh_token`, `id_token`, `account_id`. |
| `__Secure-next-auth.session-token` (and `.0`, `.1` chunks), `oai-sc` | `chatgpt.com` cookies | sidecar | Browser extraction; the collector exchanges the session cookie for a bearer via `chatgpt.com/api/auth/session`. |
| `CHATGPT_SESSION_TOKEN` | Environment variable | sidecar | Session cookie value. |
| Settings, Bearer Token / Web Session | Pasted manually | server | Bearer: the `Authorization` header of a `/wham/usage` request in DevTools. Cookie: paste the full `chatgpt.com` cookie string, including the `.0`/`.1` chunks and `oai-sc` if present. |

The Codex access token lives about ten days and its refresh token rotates on every refresh, so the server never refreshes a login a sidecar pushed. It renews when `codex` next runs, or with the sidecar's `--keep-alive` (see [Keep-alive](../sidecar.md#keep-alive-for-antigravity-claude-code-codex-and-xai-opt-in)), which refreshes a day before expiry and writes `auth.json` back.

## Endpoints

| Endpoint | Notes |
|---|---|
| `GET chatgpt.com/backend-api/wham/usage` | Plan, email and `rate_limit.{primary,secondary}_window` (`used_percent`, `limit_window_seconds`, `reset_at` or `reset_after_seconds`). A window at 0% used still renders. |
| `GET chatgpt.com/api/auth/session` | Cookie-to-bearer exchange. |

## Usage events (sidecar)

Source: `~/.codex/sessions/**/*.jsonl`, `~/.config/codex/sessions/**/*.jsonl` and `$CODEX_HOME/sessions/**/*.jsonl`, parsed by `scripts/sidecar_pkg/event_extractors/chatgpt.py`.

- **Records:** `token_count` events with usage info, reading `last_token_usage`. `turn_context` supplies the model (sticky) and reasoning effort (per turn, not carried forward); `session_meta` supplies `cwd` and git branch.
- **Event id:** Codex emits no stable message id, so the id is derived from the file stem and line number; the file stem is the session id.
- **Model:** the raw slug, lowercased and trimmed, kept verbatim (`gpt-5.3-codex`, `gpt-5.6-sol`); an empty slug is `unknown`. Pricing falls back through a family-trim chain for unseeded slugs.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No logs/auth" | Set `CHATGPT_OAUTH_TOKEN`, run `codex auth login` and the sidecar on that machine, or log in to chatgpt.com in a browser the sidecar can read. |
| 401 on `wham/usage` | The token expired: re-authenticate with the Codex CLI, enable sidecar `--keep-alive`, or set a fresh token. |
| 403 from the provider | Non-fatal; the card reports the error and the next cycle retries. |
| `make dev` shows no Codex login | The server reads no credential files; use `make dev-all`. |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/chatgpt.py` | Collector entry point and strategy table |
| `app/services/collectors/chatgpt_oauth.py` | Credential resolution and cookie-to-bearer exchange |
| `app/services/collectors/chatgpt_web.py` | `wham/usage` fetch and card shaping |
| `app/services/collectors/base.py` | Collector base class |
| `scripts/sidecar.py` | Credential rules, session discovery, event dispatch |
| `scripts/sidecar_pkg/event_extractors/chatgpt.py` | Codex usage events |
| `scripts/sidecar_pkg/codex_renewer.py` | Codex keep-alive renewer |
| `scripts/sidecar_pkg/cookie_scan.py` | Browser cookie store discovery |
