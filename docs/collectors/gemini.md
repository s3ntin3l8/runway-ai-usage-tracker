# Gemini

**File:** `app/services/collectors/gemini.py`

Google Gemini CLI quota from Google Cloud Code, plus per-message usage events the sidecar extracts from Gemini CLI session logs.

## At a glance

- **Strategies** (`STRATEGIES` in `gemini.py`): `api` only, run on the server. No enrichment strategy is declared; token breakdowns arrive as sidecar usage events (see [Strategy Types](../collection_logic.md#strategy-types)).
- **Cards:** one per model class the quota response reports (`Gemini Flash`, `Gemini Pro`, `Gemini Flash Lite`, or the raw model id), deduplicated per class, preferring the bucket that carries concrete numbers. Percent-only buckets use `unit_type: percent`; buckets with a limit use the token type.
- **Labels:** `data_source=api`; `input_source` is `sidecar` or `config` by where the OAuth bundle came from.

## Credentials

| Source | Where it comes from | Runs in | Notes |
|---|---|---|---|
| `~/.gemini/oauth_creds.json` | Gemini CLI login | sidecar | Also `<Gemini config dir>/oauth_creds.json`. Maps `access_token`, `refresh_token`, `id_token`, `expiry_date`, `client_id`. The only way a CLI login reaches the server. |
| Settings, OAuth Token | Pasted manually | server | An access token. |
| `GEMINI_OAUTH_CLIENT_ID`, `GEMINI_OAUTH_CLIENT_SECRET` | Environment | server | Optional overrides for refresh. Client id resolves from the env var, then the bundle, then the `id_token` audience, then the well-known Gemini CLI client. |

Google does not rotate Gemini's refresh token, so the server refreshes an expired access token itself (`oauth2.googleapis.com/token`) and writes it to its own token cache, never to the CLI's file.

## Endpoints

| Endpoint | Notes |
|---|---|
| `POST cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` | Project discovery (`cloudaicompanionProject`) and tier. |
| `POST cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` | Per-model quota buckets (`remainingFraction`, optional `quotaLimit`/`quotaRemaining`, `resetTime`). |

## Usage events (sidecar)

Source: `~/.gemini/tmp/<project>/chats/session-*.jsonl` (plus `~/.gemini/sessions` and `~/.config/gemini/sessions`), parsed by `scripts/sidecar_pkg/event_extractors/gemini.py`.

- **Records:** `type: "gemini"` records with a non-empty `tokens` dict (`input`, `output`, `cached`, `thoughts`). `input` includes `cached`, so fresh input is `input - cached`; `cached` is cache read and `thoughts` is reasoning. Gemini has no cache-write billing.
- **Event id:** the record's own `id`, else `<session>|line_<n>`. The file stem is the session id; the project root comes from the sibling `.project_root` file.
- **Model:** `normalize_gemini_model` maps raw names to versioned cost buckets (tier and major version), unlike the coarser pro/flash/flash-lite quota cards.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Missing gemini-3 models | `loadCodeAssist` must return a valid `cloudaicompanionProject`; the project parameter is required for them. |
| Token refresh fails | Set `GEMINI_OAUTH_CLIENT_ID` and `GEMINI_OAUTH_CLIENT_SECRET` (the values are in the Gemini CLI's `oauth2.js`). |
| Always 0% used | Expected on a generous tier. |
| `make dev` shows no Gemini login | The server reads no credential files; use `make dev-all`. |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/gemini.py` | Collector entry point and strategy table |
| `app/services/collectors/gemini_api.py` | Cloud Code quota strategy and card shaping |
| `app/services/collectors/gemini_oauth.py` | OAuth bundle handling and refresh |
| `app/services/collectors/oauth_base.py` | Shared OAuth token handling |
| `app/services/collectors/base.py` | Collector base class |
| `app/services/model_normalization.py` | Gemini model bucketing |
| `scripts/sidecar.py` | Credential rules, session discovery, event dispatch |
| `scripts/sidecar_pkg/event_extractors/gemini.py` | Gemini CLI usage events |
