# OpenCode

**File:** `app/services/collectors/opencode.py`

Quota cards for the OpenCode Go subscription (server), plus per-message usage events and session names read from OpenCode's local database (sidecar).

## At a glance

- **Strategies** (in order): `api` (OpenCode API key, bearer) then `web` (console session cookies).
- **Cards:** up to three per account, all USD: `session` (5h rolling), `weekly` (7d) and `monthly` (30d). Default limits when the API omits one: $12 / $30 / $60.
- **Labels:** the bearer endpoint is `data_source: api`, the cookie endpoint is `web`. `input_source` is `config` (key or cookies pasted in Settings), `sidecar` (discovered locally) or `server` (environment variable).
- Strategy and label semantics: [collection_logic.md](../collection_logic.md) and [Card provenance labels](../collection_logic.md#card-provenance-labels).

## Credentials

Precedence for one account: a saved Settings value, then a sidecar-discovered credential, then a server environment variable. The server never reads another host's files.

| Source | Where | Runs in | Notes |
|---|---|---|---|
| CLI key | `~/.local/share/opencode/auth.json` or `~/.opencode/auth.json`, entry `opencode-go.key` | sidecar | Key shape `oc_sk_...`. |
| OpenCode v2 key | `credential` table in `opencode.db` | sidecar | v2 imports `auth.json` once and never reads it again; the sidecar reads the table in the legacy `auth.json` shape. |
| Environment | `OPENCODE_API_KEY` (preferred), `OPENCODE_GO_API_KEY` | sidecar or server | A server uses it for the default account. |
| Settings paste | Providers, opencode, `OpenCode API Key` | server | Stored encrypted in `provider_configs`; survives restarts. |
| Cookies | `auth` and `__Host-console_session` on `opencode.ai` | sidecar (browser) or Settings paste | Both are required; a multi-cookie paste is split. Without both the console returns 401. |
| Workspace id | The account's workspace field in Settings | server | Needed only when the cookie account has several Go workspaces. |

Other providers that read the same `auth.json` (openrouter, minimax, kimi_coding, ollama, xai, ...) are covered in their own docs; this page is the reference for how that file and the v2 database are discovered.

### Account identity

An `oc_sk_` key carries no account identity, so a discovered key is shown under **Fleet, Untagged Credentials** until it is assigned. The sidecar reports it under a key-scoped origin (`path:.../auth.json#<fingerprint>` or `env:OPENCODE_API_KEY#<fingerprint>`); only the fingerprint crosses the wire. A rotated key is a new origin and needs a new tag. Background: [Credential origins and operator tags](../architecture.md#credential-origins-and-operator-tags).

The sidecar resolves the account most-specific first:

| # | Tier | Meaning |
|---|---|---|
| 0 | `OPENCODE_ACCOUNT_LABEL` | Host-local, explicit; outranks every server-side hint. |
| 1 | Keyed origin tag | Operator tag on this exact credential. |
| 1b | Fingerprint hint | `provider:opencode#<fingerprint>` when the same key is pasted in Settings. |
| 2 | Legacy plain tag | A `path:...` tag from before origins were fingerprinted. It still resolves on any host with the same path and after a key rotation, so replace it with a tag on the keyed origin. |
| 3 | Provider-wide hint | `provider:opencode`, used only when the server has one account and the local state does not contradict it. |

The tier-3 check compares the key with the CLI's own record of the active `opencode-go` account: `account.json` next to the `auth.json` the key came from, or the `credential` table on v2. If a different key is active the hint is withheld and the credential stays untagged.

To assign a key: paste it in Providers, opencode (tier 1b on the next cycle); tag its origin in Fleet, Untagged Credentials; or set `OPENCODE_ACCOUNT_LABEL=<email>` on a host whose CLI key always belongs to one account.

## Endpoints

| Endpoint | Auth | Returns |
|---|---|---|
| `GET https://opencode.ai/zen/go/v1/usage` | bearer API key | `{usage: {rolling, weekly, monthly: {percent, status, resetsAt}}}` |
| `GET https://opencode.ai/console/api/orgs` | cookies | Workspaces `[{id, name}]` |
| `GET https://opencode.ai/console/api/go/status` | cookies plus `x-org-id` | `access.meters` with used and limit (micro-cents) and `resetsAt` |

A cookie account with one Go workspace uses it automatically. With several, set the workspace id; the collector never guesses from list order. Window keys map to `window_type`: `fiveHour`/`rolling` is `session`, `week`/`weekly` is `weekly`, `month`/`monthly` is `monthly`.

## Usage events (sidecar)

The sidecar reads per-message events and session titles from `opencode.db` (read-only). It emits no quota cards from the database.

- **Location:** `~/.local/share/opencode/opencode.db` or `~/.opencode/opencode.db`. `OPENCODE_DB` (OpenCode's own override) wins when set: `~` expands, a relative path is under `~/.local/share/opencode`, and a missing target means no ingest rather than falling back.
- **Schema:** v2 is detected by the `session_v2` table (an empty `session_message` alone is not the signal); v2 migrates in place and leaves the v1 tables frozen.

| | v1 | v2 |
|---|---|---|
| Messages | `message`, `data.role = 'assistant'` | `session_message`, `type` in (`assistant`, `compaction`) |
| Model and provider | `data.modelID`, `data.providerID` | `data.model.id`, `data.model.providerID` |
| Effort | `data.variant` | `data.model.variant` (`default` means none) |
| Working directory | `data.path.cwd` | `session_v2.directory` |

- **Event id:** the message row id, so ids and watermarks survive the v1 to v2 migration without re-ingest.
- **Streaming rows:** a v2 row with no completion or error time (or a running compaction) is held back, with everything after it, until it settles. After an hour it is treated as abandoned (crashed process) and skipped. A compaction with no cost and no tokens yields no event.
- **Provider id:** OpenCode's `providerID` maps to a Runway provider: `opencode-go` to `opencode`, `opencode` to `opencode-free`, `opencode-zen`, `open-design-byok` to `opencode-byok`, `openrouter` to `opencode-openrouter`, `ollama-cloud` to `opencode-ollama`, any other id to `opencode-<id>`. A backend Runway collects directly (for example xai) is retagged onto that provider, and a gateway-tagged message with an OpenRouter-only model id folds onto `openrouter`.
- **Session names:** the sidecar reads `session_v2.title` (v2, which wins even when blank) or `session.title` (v1) for sessions that have assistant messages. See [Session names from the sidecar](../collection_logic.md#session-names).
- **Repair:** `scripts/reclassify_opencode_providers.py` re-applies the provider mapping to already ingested events and reads both layouts.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Error card "OpenCode session expired" | Key missing or rejected, or cookies missing or stale. Paste a fresh `oc_sk_` key in Providers, opencode, or make `auth.json` readable by the sidecar. |
| Error card "no workspace found" | The cookie account has no Go workspace; check the console login. |
| Error card "Set the OpenCode workspace ID" | Several Go workspaces; enter the id in the account settings. |
| Error card "usage API unreachable" | Transient; retried next cycle. |
| Key appears under Untagged Credentials | Assign it as described under Account identity. |
| Key tagged but lands on the wrong account | A legacy plain tag is still in force; tag the keyed origin and delete the legacy row under Fleet, Credential mappings. |
| No events on a v2 host | Check `OPENCODE_DB` and that the sidecar can read `opencode.db`; a stuck streaming row holds later rows back for up to an hour. |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/opencode.py` | Quota collector |
| `scripts/sidecar.py` | Credential rules, account resolution, event and session-name dispatch |
| `scripts/sidecar_pkg/opencode_credentials.py` | v2 `credential` table reader and key binding |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Usage events and session titles |
| `scripts/reclassify_opencode_providers.py` | Provider reclassification of ingested events |
| `app/core/registry.json` (`providers.opencode`) | Credential rules and UI help text |
