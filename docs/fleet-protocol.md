# The fleet protocol

How a **sidecar** and the **Runway server** talk to each other. The sidecar
runs on a workstation, reads local credentials and usage, and pushes them to
the server. The server stores them, calls the providers' APIs with the
credentials it holds, and pushes remote control back.

This document describes the protocol in one place. The pieces are implemented
in `app/api/endpoints/fleet.py` and `app/api/endpoints/fleet_sources.py`, and
on the client side in `scripts/sidecar.py` and `scripts/sidecar_pkg/`.

> Every fleet route lives under the `/api/v1/fleet` prefix.

## Authentication

Two independent schemes.

### HMAC ingest auth — for the data plane

The sidecar signs its payloads with the shared ingest key (`INGEST_API_KEY`).
Three headers:

| Header | Value |
|---|---|
| `X-Signature` | `HMAC-SHA256(secret, timestamp + body)` |
| `X-Timestamp` | Unix timestamp. It must fall inside `[now - 300, now + 60]` — five minutes of clock skew in the past, one minute in the future. Outside that window the request is rejected with **400**. |

The server validates the signature and **claims** it. A claimed signature is
rejected on replay — receipts are held for 360 seconds and contain neither the
request body nor reusable signature material. An invalid request also consumes
a valid signature, so a retry must sign a fresh timestamp.

HMAC auth applies to exactly two routes: `POST /ingest` and
`POST /credentials/manifest`. Every other fleet route is admin-authenticated
or device-authenticated, with one exception: `GET /fleet/config` is neither —
it is rate-limited (`60/minute`) and returns only the enabled/strategies view
to anyone, unless the caller signs the request with the ingest key or the
server is bound to loopback. This split is enforced by
`tests/unit/test_forward_auth_routes.py`.

### Device identity — for enrolled sidecars

An enrolled sidecar has a random HMAC secret of its own, stored encrypted.
Source identity is **(device, scraper, origin/account, generation)** —
hostnames are labels, not identity. Legacy shared-key clients stay compatible
but cannot impersonate an enrolled device.

Withdrawal deletes the matching secret cache material and persists a
tombstone, so late ingest cannot resurrect a withdrawn source. Re-enabling
uses a **newer generation**. Historical `usage_events` and other contributions
survive a withdrawal.

### Admin auth

Fleet management routes (listing sidecars, pausing, pairing, credential
tags) require the admin key or a trusted forward-auth proxy.

## Pairing

A new machine is enrolled by pairing:

1. An operator generates a **pairing code** in the dashboard
   (`POST /fleet/pairing-codes`). The code is one-time and expires after
   `PAIRING_CODE_TTL_SECONDS` (default 600 s).
2. The operator opens the `runway-sidecar://pair?…` link on the new machine,
   or runs `runway-sidecar-cli --pair SERVER_URL CODE`.
3. The sidecar posts the code to `POST /fleet/pair` and receives its device
   secret and the server configuration.
4. It writes `api_url` / `api_key` to its config and exits; start the daemon
   afterwards.

Only pair with a link generated in **your own** dashboard — pairing points that
machine's usage data and provider sign-ins at that server.

## The ingest cycle

`POST /fleet/ingest` is the sidecar's heartbeat. One cycle:

1. The sidecar classifies every browser cookie store and every credential
   source it can reach.
2. It collects quota cards and per-message events.
3. It signs the payload and posts it.
4. The server verifies the signature, stores the credentials in an in-memory
   cache (30-minute TTL), upserts the cards into `latest_usage`, and ingests
   the events into `usage_events`.

**Dedup key:** events are deduplicated by `(provider_id, event_id)` — *not* by
account. See [`migration-v3.md`](migration-v3.md).

The response tells the sidecar what to do next:

| Field | Effect |
|---|---|
| `poll_providers` | Which providers to collect, and at which interval. It lists the enabled providers, the passive ones (cards present but no config row) and the sidecar-only event providers (`hermes`, `minimax_code`). The last is a scheduling id only — it is never shown as a provider. |
| `sidecar_auto_update` / `sidecar_update_channel` | The fleet-wide update policy, and the per-sidecar override that wins over it. |
| `keep_alive_desired` | Whether the keep-alive thread should run. The sidecar starts or stops it without a restart. |
| `log_level` | A live log-level override from the dashboard. Applied immediately, no restart; it is not written to the config. |

The polling cadence is **server-controlled**. There is no local
`interval_seconds` or `providers` config key.

## Credential manifest

`POST /fleet/credentials/manifest` reports what the sidecar can see — which
credentials, from which origins, and which sources are blocked.

A blocked source travels as one entry per provider that wanted a cookie from
it:

```json
{
  "provider_id": "ollama",
  "credential_origin": "cookie:ollama/Chrome/Default/Network/Cookies",
  "reason": "source_locked",
  "source_store": "Chrome/Default/Network/Cookies",
  "source_detail": "held open by another process",
  "source_remedy": "set OLLAMA_SESSION_TOKEN (or paste the cookie in Settings → Providers)"
}
```

`reason` is one of `source_locked` (the browser holds the store open),
`source_unsupported` (the values are app-bound encrypted) or
`source_unreadable` (the file could not be read for another reason). The entry
carries **no cookie value, no account id and no filesystem path**.

The report's lifecycle is deliberately *not* a credential row's lifecycle: the
sidecar repeats it every cycle while the store stays unreadable, and the server
drops it as soon as the sidecar stops reporting that store.

## Credential tags

`POST /fleet/credentials/tags` lets a sidecar propose an account label for a
credential it found — for example an email address that identifies the Claude
account behind an opaque credential id.

Tags are a proposal, not a decision. The operator assigns them; the server
records the mapping. `GET /fleet/credentials/tags` lists them,
`DELETE /fleet/credentials/tags` removes one, and
`GET /fleet/credentials/tags/pending` lists the tags still waiting.

## Pending events

An event pushed with no resolvable account identity is **held** in a pending
queue rather than dropped.

| Route | Purpose |
|---|---|
| `GET /fleet/events/pending` | List pending events. |
| `GET /fleet/events/pending/sessions` | The same, grouped by session. |
| `POST /fleet/events/pending/assign` | Assign one pending event to an account. |
| `POST /fleet/events/pending/assign-batch` | Assign many at once. |

Assignment needs evidence: the operator picks the account, and the server
records it. This is why the Home dashboard links to Fleet when pending events
exist.

## Remote control

The server drives the sidecar; the sidecar polls. There are no inbound
connections to the workstation.

| Route | Effect |
|---|---|
| `PATCH /fleet/sidecars/{id}` | Update a sidecar's registration. |
| `PUT /fleet/sidecars/{id}/settings` | Change per-sidecar settings (update policy, channel, keep-alive, login directories). |
| `PUT /fleet/sidecars/{id}/keep-alive` | Set the keep-alive switch for one machine. |
| `POST /fleet/sidecars/{id}/pause` | Pause collection. |
| `POST /fleet/sidecars/{id}/resume` | Resume — **only** a server-imposed pause, within existing local consent. |
| `POST /fleet/sidecars/{id}/update` | Ask the sidecar to self-install on its next heartbeat. |
| `POST /fleet/sidecars/{id}/sources/{source_id}/commands` | Send a command to one source. |

Commands use durable ids, ordered generations and idempotent acknowledgments
over sidecar polling.

### What the server may and may not do

| The server **may** | The server **may not** |
|---|---|
| Narrow a source's permission | Enable local access or writes |
| Pause and resume collection | Grant local consent |
| Revoke a contribution | Enable a scraper the operator did not enable |
| Request local setup | Perform that setup — it can only create a notice, never a native prompt |

A setup request creates a **notice**. It never opens a permission prompt on the
operator's machine. See [`source-consent.md`](source-consent.md).

## Source lifecycle

`GET /fleet/sidecars/{id}/sources` reports the sources on one machine, and
`POST /fleet/source-sync` synchronises source state across the fleet.

Local consent, authorization, remote pause, collection health, renewal
support, last success and report time are all distinct. **Offline/stale
reporting is not the same as server-side collection health** — a sidecar that
has not checked in still holds its credentials, and the server keeps using
eligible ones.

## Route index

| Method | Route |
|---|---|
| POST | `/ingest` |
| POST | `/credentials/manifest` |
| POST / GET / DELETE | `/credentials/tags` |
| GET | `/credentials/tags/pending` |
| GET / POST | `/credentials/{source_id}/claude-organizations` |
| GET | `/events/pending` |
| GET | `/events/pending/sessions` |
| POST | `/events/pending/assign` |
| POST | `/events/pending/assign-batch` |
| GET | `/sidecars` |
| GET / PATCH / DELETE | `/sidecars/{sidecar_id}` |
| PUT | `/sidecars/{sidecar_id}/settings` |
| PUT | `/sidecars/{sidecar_id}/keep-alive` |
| POST | `/sidecars/{sidecar_id}/pause` |
| POST | `/sidecars/{sidecar_id}/resume` |
| POST | `/sidecars/{sidecar_id}/update` |
| GET | `/config` |
| POST | `/pairing-codes` |
| POST | `/pair` |
| POST | `/source-sync` |
| GET | `/sidecars/{sidecar_id}/sources` |
| POST | `/sidecars/{sidecar_id}/sources/{source_id}/commands` |

## Related

- [`api-reference.md`](api-reference.md) — every route, with its auth scheme.
- [`SECURITY.md`](SECURITY.md) — the trust model these routes implement.
- [`source-consent.md`](source-consent.md) — what may be read, and who says so.
- [`sidecar.md`](sidecar.md) — the sidecar side of the protocol.
- [`frontend.md`](frontend.md) — the Fleet page that drives it.
