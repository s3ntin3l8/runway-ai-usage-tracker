# The fleet protocol

How a **sidecar** and the **Runway server** talk to each other. The sidecar
runs on a workstation, reads local credentials and usage, and pushes them to
the server. The server stores them, calls the providers' APIs with the
credentials it holds, and pushes remote control back.

This document describes the protocol in one place. The pieces are implemented
in the `app/api/endpoints/fleet_*.py` modules (ingest, credentials, pending
events, sidecars, pairing and their shared helpers) and in
`app/api/endpoints/fleet_sources.py`, and
on the client side in `scripts/sidecar.py` and `scripts/sidecar_pkg/`.

> Every fleet route lives under the `/api/v1/fleet` prefix.

## Authentication

Three independent schemes.

### Shared-key HMAC — the data plane for legacy clients

The sidecar signs its payloads with the shared ingest key (`INGEST_API_KEY`).
Two headers:

| Header | Value |
|---|---|
| `X-Signature` | `HMAC-SHA256(secret, timestamp + body)` |
| `X-Timestamp` | Unix timestamp. It must fall inside `[now - 300, now + 60]` — five minutes of clock skew in the past, one minute in the future. Outside that window the request is rejected with **400**. |

The server validates the signature and **claims** it as a durable, single-use
receipt. Reusing the same envelope returns **409** (`replayed_request`), and
the receipt holds neither the request body nor reusable signature material.
Every retry must therefore sign a fresh timestamp.

Signed requests are accepted on `POST /ingest`, `POST /credentials/manifest`,
`POST /source-sync` and `GET /config`. `GET /config` is also served unsigned to
anyone, rate-limited (`60/minute`), as the redacted enabled/strategies view
unless the caller signs it with the ingest key or the server is bound to
loopback. Every other fleet route is admin-authenticated, or open by design
(`POST /pair`, authorised by the one-time code). The split is enforced by
`tests/unit/test_forward_auth_routes.py`.

### Device identity — for enrolled sidecars

Pairing enrols a sidecar with a random HMAC secret of its own, stored
encrypted. Source identity is **(device, scraper, origin/account,
generation)** — hostnames are labels, not identity. A device request sends
`X-Runway-Device-Id`, `X-Timestamp` and `X-Signature`, where the signature is
HMAC-SHA256 under the device key over
`DEVICEv1|device_id|timestamp|purpose|` followed by the raw body (the raw query
bytes for `GET /config`). `purpose` is `ingest`, `manifest`, `config` or
`source-sync`, so a signature cannot be replayed against another operation
and does not depend on a proxy preserving the public path. The timestamp
window, body caps and replay protection are the same as above.

A shared ingest key cannot authenticate a device header or claim a reserved
`device-<32 hex>` registry id, so legacy clients stay compatible but cannot
impersonate an enrolled device. A sidecar whose enrolment is lost or revoked
sees a 401 from `source-sync`, shows a re-pair notice and withholds Keychain
credentials; it never falls back to the shared key.

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
3. The sidecar posts the code to `POST /fleet/pair`. The response carries
   `api_url` and `api_key`; when the server has `DB_ENCRYPTION_KEY`, it also
   carries `device_id`, `device_key`, `device_api_url` and `source_protocol`.
   Without it, pairing returns the legacy response and Keychain forwarding
   stays limited until enrolment.
4. It writes these to its config (device key in an owner-only file) and exits;
   start the daemon afterwards. Re-pairing creates a new device; it does not
   claim the hostname's earlier sources.

Only pair with a link generated in **your own** dashboard — pairing points that
machine's usage data and provider sign-ins at that server.

## The ingest cycle

`POST /fleet/ingest` is the sidecar's heartbeat. One cycle:

1. The sidecar classifies every browser cookie store and every credential
   source it can reach.
2. It collects quota cards and per-message events.
3. It signs the payload and posts it.
4. The server verifies the signature, caches the credentials (a 30-minute
   TTL for shared-key clients; contributions from enrolled devices are durable
   and survive a server restart or an offline machine), upserts the cards into `latest_usage`, and ingests
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

When a credential's origin has no known account, it waits as *pending*. The
**operator** resolves it with `POST /fleet/credentials/tags` (admin-only),
choosing an account that is configured or has live evidence for the same
provider. `GET` lists tags, `DELETE` removes one and
`GET /fleet/credentials/tags/pending` lists the origins still waiting. A tag
is scoped to the deployment or to one sidecar; browser-cookie and Keychain
origins belong to a machine and take a sidecar scope. Tagging to an archived
account parks the binding until the account is restored.

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
| `POST /fleet/sidecars/{id}/adopt` | Fold the legacy sidecar a re-paired machine used to report as into its enrolled card (history, tags, settings); `dry_run` previews. |
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

In the dashboard these live on the **Credentials** tab of a sidecar's settings: a ⋯ menu per
source (pause or resume, ask the Mac to set up, revoke after a confirmation), and bulk
**Ask Mac to set up N**, **Pause all** and **Resume all**. Every one of them only sends the commands above.

## Source lifecycle

`GET /fleet/sidecars/{id}/sources` reports the sources on one machine, and
`POST /fleet/source-sync` synchronises source state across the fleet.

Local consent, authorization, remote pause, collection health, renewal
support, last success and report time are all distinct. **Offline/stale
reporting is not the same as server-side collection health** — a sidecar that
has not checked in still holds its credentials, and the server keeps using
eligible ones.

## What the sidecar forwards

Forwarded: quota cards, per-message usage events (batched, 1000 per push),
credentials the server needs to call provider APIs, and a credential manifest.
Usage events carry token counts, model, timestamps and ids; conversation text
and raw transcript records are not uploaded. The sidecar extracts and pushes;
the server makes every provider call. When the server is unreachable the
payload goes to the sidecar's offline queue and is replayed later; events are
deduplicated by id, so a replay cannot double-count.

## Related

- [`api-reference.md`](api-reference.md) — every route, with its auth scheme, and the device-enrolment details.
- [`SECURITY.md`](SECURITY.md) — the trust model these routes implement.
- [`source-consent.md`](source-consent.md) — what may be read, and who says so.
- [`sidecar.md`](sidecar.md) — the sidecar side of the protocol.
- [`frontend.md`](frontend.md) — the Fleet page that drives it.
