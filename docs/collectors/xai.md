# xAI (Grok) Collector

**File:** `app/services/collectors/xai.py`

Collects the consumer Grok subscription's credit balance and on-demand usage through xAI's CLI chat proxy. The OpenAI-compatible `XAI_API_KEY` developer API is a separate product and is not used by this collector.

## Authentication

The collector reads the access bearer from `xai_access`. The sidecar can obtain it from:

- OpenCode `auth.json`, mapping `xai.access` to `xai_access` and `xai.refresh` to `xai_refresh`.
- Grok CLI `~/.grok/auth.json` (or `$GROK_HOME/auth.json`), selecting an xAI OIDC scope entry and mapping `key` to `xai_access` and `refresh_token` to `xai_refresh`.
- `GROK_OAUTH_TOKEN`, mapped to `xai_access`.
- A bearer pasted into provider settings, stored as `provider_configs.api_key` and mirrored into the token cache's `xai_access` slot.

All three sidecar sources get key-scoped origins (#349), e.g.
`path:/home/u/.local/share/opencode/auth.json#495fa9c614ce` or
`env:GROK_OAUTH_TOKEN#495fa9c614ce`. The suffix comes from `xai_refresh`
when the candidate has it and only falls back to `xai_access`, because the
access JWT is the part that expires (~7 days) — the Grok / OpenCode CLI
refreshes it behind Runway's back, and keying on it would mint a new origin
every week, stranding the tag that was written against the old one. File
and CLI candidates ship the refresh token (`xai.refresh` in OpenCode's
`auth.json`, `refresh_token` in `~/.grok/auth.json`) and are keyed by it;
the access-only `GROK_OAUTH_TOKEN` candidate has nothing else to go on and
is keyed by its bearer. See *The sibling providers (#349)* in
[opencode.md](opencode.md).

That split bounds the server-side hint: `provider:xai#<fingerprint>` is
built from the pasted `provider_configs.api_key`, which is an *access*
bearer, so it answers for the env candidate (and for a file candidate only
while the paste still equals its `xai_access`) — never for a refresh-keyed
origin. Tag a file- or CLI-sourced card against its keyed origin in **Fleet
→ Untagged Credentials** instead; that tag then survives every access
refresh and is stranded only when the refresh token itself rotates.

Runway does not refresh these tokens. For Grok CLI credentials, both the quota card and usage events use the selected scope entry's email as the account identity, then its user ID. If neither is available, the normal credential tagging flow lets the operator associate the credential with an account.

The collector checks a readable JWT expiry before sending requests. An expired token or a rejected request produces an `auth_failed` card that points to the CLI re-login flow.

## Usage history

The sidecar reads completed turns from `~/.grok/sessions/<url-encoded-cwd>/<session-id>/updates.jsonl`. It ignores `signals.json`: `totalTokensBeforeCompaction` is a cumulative sum of context tokens at compaction and is not session usage.

Each completed turn with usage produces stable event IDs from session, prompt, and model. If `modelUsage` is present, the sidecar emits one event per model; otherwise it uses turn totals and the latest model selection recorded in the update stream. Input, cache-read, cache-create, output, and reasoning buckets are split so cached input and reasoning are not counted twice. Reported cost is included only when usage is complete and cost is not marked partial. Replays include a one-second watermark overlap so turns sharing a timestamp are retained; stable event IDs let the server deduplicate them.

The bootstrap window comes from `SIDECAR_BOOTSTRAP_DAYS` (90 days by default). OpenCode events tagged with `providerID: "xai"` are also retagged onto `provider_id: "xai"`.

Grok usage pricing coverage is tracked separately in [issue #346](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/346).

## Endpoints

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `https://cli-chat-proxy.grok.com/v1/billing?format=credits` | GET | Bearer OAuth token plus `x-xai-token-auth: xai-grok-cli` | Credits and on-demand usage |
| `https://cli-chat-proxy.grok.com/v1/settings` | GET | Same | Best-effort subscription tier enrichment |

## Card schema

The collector emits a credits card (`weekly` or `monthly` according to `currentPeriod.type`) and an optional on-demand card when the account has a non-zero `onDemandCap`. The on-demand card reports currency in USD. Subscription tier enrichment from `/v1/settings` is best effort.

## Failure modes

| Reason | Error type | Card message |
|---|---|---|
| `invalid_api_key` | `auth_failed` | xAI session expired; re-login with the Grok or OpenCode CLI |
| `parse_error` | `parse_error` | xAI quota response could not be parsed |
| Timeout or network failure | Base collector retry | No error card |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/xai.py` | Quota collector |
| `scripts/sidecar.py` | Credential dispatch, identity selection, and session discovery |
| `scripts/sidecar_pkg/event_extractors/xai.py` | Completed-turn event extraction |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Retags OpenCode events tagged with `providerID: "xai"` |
| `app/core/registry.json` (`providers.xai`) | UI credential instructions and sidecar rule mirror |

## References

- [xAI](https://x.ai)
- [Grok CLI usage signals](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-shell/src/session/signals.rs)
- [Grok CLI session update schema](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-shell/src/extensions/notification.rs)
- [OpenCode CLI](https://opencode.ai)
