# MiniMax

**File:** `app/services/collectors/minimax.py`

Server collector for the MiniMax coding-plan quota (5-hour and weekly windows). MiniMax Code
*usage events* are a separate, sidecar-only path: see [minimax_code.md](minimax_code.md).

## At a glance

- **Strategy:** `api` only. `MiniMaxCollector` has no `STRATEGIES` dict and no fallback tier
  (`_fallback_strategies()` returns `[]`): the coding-plan web page is a client-rendered SPA
  and is not scraped.
- **Cards:** one `session` card (label `5h`) per entitled quota bucket plus one `weekly` card.
  There is no monthly window. `COMPLETE_SNAPSHOT = True`, so a missing window retires the old
  card.
- **Labels:** `data_source: "api"`; `input_source` is `config` (Settings or a manually pasted
  credential), `sidecar` (credential pushed by a sidecar) or `server` (`MINIMAX_API_KEY` env,
  default account only). Values are defined in [Card provenance labels](../collection_logic.md#card-provenance-labels). The card
  `detail` ends in `[API]` or `[App]` according to the credential kind.
- **Two credential kinds** reach this collector and each needs its own host (see below).

## Credentials

The kind is decided by the token prefix, so a credential pasted into Settings or stored in the
database still goes to the right host.

| Kind | Looks like | Source | Runs in | Notes |
|---|---|---|---|---|
| Platform API secret | `sk-…` (anything not `mmoat_`) | `MINIMAX_API_KEY` env; Settings; opencode `auth.json` (`minimax-coding-plan.key`) | server (env, Settings); sidecar (opencode file) | Sent to the API host |
| MiniMax Code app OAuth token | `mmoat_…` | `~/.minimax/auth/*/*/mcode-public/auth.json` (`records.*.accessToken`, with refresh token, client id and expiry) | sidecar only | Sent to the open-platform host |

Lookup order in `_get_current_creds()`: stored provider config, then the sidecar token cache
(`api_key` slot, else `oauth_token`), then `MINIMAX_API_KEY` for the `default` account.

The app's auth store keeps one record per build and region, each with its own expiry. Server
and sidecar both pick the record whose expiry is still in the future, and the token and expiry
come from the same record. The expiry unit (seconds to nanoseconds) is read from the number's
magnitude, not from the field name: see [Expiry units](../collection_logic.md#expiry-units).
The failure mode when the wrong kind is sent is described in
[collection_logic.md](../collection_logic.md) ("MiniMax credentials").

| Variable | Default | Description |
|---|---|---|
| `MINIMAX_API_KEY` | none | Platform API secret for the default account |
| `MINIMAX_HOST` | `minimax.io` | Bare domain; `minimaxi.com` selects the China platform. A value with a scheme is reduced to its host |

## Endpoints

| Credential | Method and URL | Auth |
|---|---|---|
| API secret | `GET https://api.<host>/v1/coding_plan/remains` | `Authorization: Bearer <key>` |
| OAuth token, global | `GET https://platform.<host>/v1/api/openplatform/coding_plan/remains` | `Authorization: Bearer <token>` |
| OAuth token, China | `GET https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains` | `Authorization: Bearer <token>` |

The China open-platform host is chosen when the parsed hostname of `MINIMAX_HOST` is
`minimaxi.com` or a subdomain of it (a plain suffix test would accept `evilminimaxi.com`).
Both paths return the same payload, so one parser serves both. The dashboard link on the
cards is `https://platform.minimax.io/user-center/payment/coding-plan`.

Payload shape (abridged):

```json
{
  "model_remains": [
    {"model_name": "general",
     "end_time": 1788379200000,
     "current_interval_total_count": 0, "current_interval_usage_count": 0,
     "current_interval_status": 1, "current_interval_remaining_percent": 100,
     "weekly_end_time": 1788739200000,
     "current_weekly_total_count": 0, "current_weekly_usage_count": 0,
     "current_weekly_status": 1, "current_weekly_remaining_percent": 100}
  ],
  "base_resp": {"status_code": 0, "status_msg": "success"}
}
```

Parsing rules (`_parse_api_response`):

- `model_remains[]` holds one entry per quota bucket, not per billable model. `general` is the
  text/coding bucket. A `video` bucket reports status 3 and is ignored.
- Status 1 (available) and 2 (exhausted, 0% remaining) mean the bucket is part of the plan
  (`ENTITLED_STATUSES`); anything else is skipped. Session and weekly entitlement are gated
  independently, and the single account-level weekly card is built from the first entitled
  entry.
- Usage is `usage_count / total_count` once `total_count > 0`; before that the card uses
  `100 - remaining_percent` and shows `—/— prompts`.
- No entitled bucket yields one healthy "No active plan" card.
- HTTP 200 with a non-zero `base_resp.status_code` is a failure, not an empty plan. Code 1004
  ("login fail") becomes an `auth_failed` error card, other codes an `api_error` card.

## Token lifetime and keep-alive

The MiniMax Code access token lives **twelve hours**, and only the MiniMax Code CLI or the
desktop app renews it — when one of them next runs. A host where neither runs leaves the login
expired, and the collector's app-token requests fail with `auth_failed` ("MiniMax error 1004:
login fail") until the user signs in again.

With the sidecar's [keep-alive](../sidecar.md#keep-alive-for-antigravity-claude-code-codex-minimax-code-and-xai-opt-in)
on, the sidecar renews the login itself, 90 minutes before the token lapses, and writes the result
back into the app's own store (`scripts/sidecar_pkg/minimax_code_renewer.py`). It refreshes at the
account service of the login's region — `https://account.minimax.io/oauth2/token` for `en`,
`https://account.minimax.cn/oauth2/token` for `cn` — with the CLI's own request
(`grant_type=refresh_token`, `refresh_token`, `client_id=mcode-public`, `scope=agent.default`,
`audience=agent-backend`).

The app keeps its login in two files, `auth.json` (the credential) and `auth-state.json` (the
state), whose `generation` counters must match — the app answers "login error" when they do not.
The write-back therefore updates only the token, the expiry and the status, and never bumps the
generation. If
the app refreshes at the same moment, the sidecar stands down (the state says `refreshing`, or a
credential is staged in `refresh/`) and drops its own result when the app got there first.

### Live behaviour (checked 2026-11-10, `en` region)

The renewer's request was run once against the live account service, through the renewer's own
`request_refresh` and `write_back`. It ran on the operator's own login, in use at the time (a
MiniMax Code CLI session was running on it), with a backup taken first — no throwaway account was
used, so the issue's throwaway-login condition was not met. Only the `en` host received a real
refresh token:

- **The request works.** `POST https://account.minimax.io/oauth2/token` answered with
  `access_token`, `refresh_token`, `token_type: Bearer`, `expires_in: 43200` and
  `scope: agent.default`. Negative probes with a bogus refresh token (and, for `invalid_client`, a bogus
  `client_id`) were sent to both `account.minimax.io` and `account.minimax.cn`: an unknown
  `client_id` is refused with `invalid_client`, and a dead refresh token with `400 invalid_grant`
  ("this refresh token can no longer be used"). The `cn` host was probed this way only; no real
  login was refreshed there.
- **The refresh token rotates.** The response carried a new one and the old one was rejected
  straight away, so a refresh that is not written back loses the login.
- **The store accepts the write.** A running MiniMax Code CLI on the same login picked up the
  written token within seconds, refreshed from it, and committed `generation` 89 → 90 on both files
  with status `authenticated`. This is one observation, not proof the CLI reacted to the write
  rather than refreshing on its own.
- A fresh access token worked against `GET platform.minimax.io/v1/api/openplatform/coding_plan/remains`
  (the app-token endpoint, not the API-secret one). By then the CLI had refreshed again, so this
  was the CLI's token rather than the one the sidecar wrote.

Not yet observed: the desktop app's behaviour after a sidecar renewal, a renewal that lands while
the app is mid-refresh (the window between the app's read and its commit is not closed by the
`refreshing` / `refresh/` checks), and the refresh token's own lifetime (it needs an idle login).

Because the refresh token rotates, the exchange spends the old one before `write_back` re-reads the
store. If `write_back` then returns `superseded` (the credential or generation moved, the state is
not renewable, or the app is mid-refresh), the new refresh token is dropped while the old one is
already dead: that login is signed out unless the app's own refresh produced a valid one. The
decision in #1306 about keeping the MiniMax Code keep-alive opt-in rests on this.
The OS-keyring store (`storeKind: "os-keyring"`) is not covered either.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Missing API key" error card | No credential found. Set `MINIMAX_API_KEY`, paste a key in Settings, or run a sidecar on a host that has the opencode key or the MiniMax Code login. |
| "MiniMax error 1004: login fail" | The credential reached the wrong host. Check that an `sk-` key and an `mmoat_` token were not swapped or edited so the prefix no longer matches. For the China platform also set `MINIMAX_HOST=minimaxi.com`. |
| "API connection failed" | Network error, non-200 status or a rejected key; the HTTP status is in the server log (`MiniMax API error`). |
| "No active plan" although a plan exists | Every bucket reported a status other than 1 or 2, or the key belongs to a different region. Check `MINIMAX_HOST`. |
| App token stops working after some time | The app login expired. Sign in to MiniMax Code again; the sidecar only forwards a record whose expiry is still in the future. With the sidecar's keep-alive on, the sidecar renews the login before it lapses — see [Token lifetime and keep-alive](#token-lifetime-and-keep-alive). |
| MiniMax usage from OpenCode missing from the cards | See [opencode.md](opencode.md): `minimax-coding-plan` events are retagged to `minimax` at ingest. |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/minimax.py` | Collector: credential kind, host selection, parser |
| `app/services/collectors/base.py` | Collector base class |
| `app/core/registry.json` | `minimax` credential rules (env, opencode file, app login file) |
| `scripts/sidecar_pkg/minimax_code_renewer.py` | Sidecar keep-alive: renews the app login in place |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Retags OpenCode `minimax-coding-plan` events onto `minimax` |
