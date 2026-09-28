# Migrating to v3.0.0

## Before you upgrade

**Back up the database first.** `~/.config/runway/runway.db` (or your
`RUNWAY_CONFIG_DIR`) — a plain file copy is enough if the server is stopped;
otherwise use `sqlite3 runway.db "VACUUM INTO 'runway.db.bak'"` so you get a
consistent snapshot without stopping anything. The first boot after
upgrading runs a one-time, one-way cleanup (below) that deletes
cross-account duplicate events; there's no automatic rollback if something
about your data trips it up.

## First boot after upgrading

`usage_events` used to be unique on `(provider_id, account_id, event_id)`,
so the same message pushed under a second account (an operator retag, a tag
hint that arrived after the fact) was inserted again and double-counted.
The very first startup on an existing database now collapses those
duplicates and rebuilds the affected rollups from the surviving events —
this is what makes `(provider_id, event_id)` alone unique going forward.
It's a one-shot migration (a no-op on every later boot) and, even on a
database with hundreds of thousands of events, is a matter of seconds, not
minutes — the server does not accept requests until it finishes. Closed
historical windows (the frozen totals in `usage_windows`) are not
recomputed; a window that closed while the double-count was live keeps its
inflated total.

## Events held back under an unresolved identity

Previously, an event pushed with no resolvable account identity (no
saved credential to match it to, no operator tag yet) was attributed to a
`default` catch-all account and counted right away. As of `#359`, that
event is instead held in a pending queue — excluded from account totals —
until an admin assigns it. This is more conservative (nothing gets silently
mis-attributed to `default` and mixed with other accounts' totals) but
means usage that previously appeared automatically may now sit unassigned
after upgrading.

- The Fleet page shows an **Unassigned usage** banner whenever the queue is
  non-empty, with a picker per event.
- The account picker only offers accounts that already have a
  `provider_configs` row (Settings → Providers) for that specific provider —
  if the account you want isn't listed, add it there first.
- **If a configured account is still keyed `account_id="default"`**
  (label may show your email, but the row's own id is `default`), you'll
  see it in the picker labeled `<label> (default)` — a hint, not a
  requirement, that assigning there keeps the event on the shared `default`
  bucket rather than its own account. Nothing stops you from choosing it
  deliberately if that's what you want; re-key the config to its real
  account id first if it's not (Settings → Providers → that provider →
  Account ID).
- Nothing forces the queue empty. Events sit there safely (excluded from
  totals) for as long as you leave them.

## Open Settings → Data health after upgrading

v3.0.0 ships an in-app **Data health** page (Settings → Data health) that
scans for the same data-quality issues this release's own prod cleanup
turned up: a provider config still keyed `default` after its label picked
up a real identity, events stuck under a legacy OpenCode-sibling provider
id, events sitting alone under a stale `default` account with a real
account configured elsewhere, credential tags and gauge-series cards left
behind by an account move, models priced at $0 with no seed row, and
`usage_period_rollup` drift from a direct database edit. Each finding shows
a preview before you apply anything, and checks with dependencies (e.g. a
default-keyed config) block the checks downstream of them until fixed. See
[docs/data-health.md](data-health.md) for the full list and what each fix
does. Worth a look right after upgrading, especially if you've run
previous versions long enough to accumulate account or provider-id drift.

## Credential-health alerts are on by default

Existing Discord/Slack webhooks configured before this release will start
firing when a credential they watch goes `expired` or `invalid` — a new
`credential_alerts` column defaults to `true` for every existing row, not
just newly created ones. Turn it off per webhook in Settings → Webhooks if
you only want the original threshold alerts.

## Known issue: credential alerts can double-fire

Overlapping poll cycles (e.g. a manual `POST /force-collect` landing while a
scheduled poll is already running) can occasionally deliver the same
credential-health alert to a webhook twice. Tracked in
[#366](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/366); not
a regression from this release, and not something that loses or
double-counts usage data — only a possible duplicate notification.

## `/fleet/config` returns less to unsigned callers

On a non-loopback bind, `GET /api/v1/fleet/config` now returns only the
`enabled`/`strategies` view to callers that don't sign the request (or
aren't hitting `127.0.0.1`) — account ids, tag hints, and credential tokens
are withheld. Sidecars built for this release sign every request and are
unaffected; a third-party integration reading this endpoint directly for
account-level detail will need to add the signature (see
`verify_config_signature` in `app/api/endpoints/fleet.py`) or run against a
loopback bind.

## Stable-channel sidecar self-update

If your sidecar is on the stable release channel (not `edge`), one manual
step is needed to reach v3.0.0: download and run the new installer once
(same as any release with a major version bump). After that, normal
self-update resumes — this release also fixes the updater to correctly
find stable-channel release assets, which it previously couldn't.

## Non-localhost binds now require an admin gate

Previously, leaving `ADMIN_API_KEY` unset made every caller admin —
including on a bind reachable from the network. Runway now refuses to
start on a non-localhost `APP_HOST` unless **either** `ADMIN_API_KEY` **or**
`TRUSTED_PROXY_IPS` is set (localhost binds are unaffected). A forward-auth
deployment that already sets `TRUSTED_PROXY_IPS` needs no changes. A plain
network deployment with neither set must add `ADMIN_API_KEY` before
upgrading, or the server exits at startup with a `RuntimeError` naming this
gate. See [SECURITY.md → Multi-Host Startup Gates](SECURITY.md).

Separately, a forward-auth deployment that has pointed `FORWARD_AUTH_USER_HEADER`
at something other than the default `X-Forwarded-User` (e.g. Authentik's
`X-authentik-username`) no longer falls back to a bare `Remote-User` header.
If your proxy only ever sends the configured header, this changes nothing;
if you were relying on `Remote-User` as a fallback alongside a custom
header name, switch to sending the configured header instead.

`POST /api/v1/usage/reset/{provider}`, `POST /api/v1/usage/collect/{provider}`,
and the GitHub device-flow endpoints (`/api/v1/auth/github/{init,poll,logout}`)
already used the admin-key dependency, but on a network bind without
`ADMIN_API_KEY` the old bypass granted admin access to every caller. They now
require a real admin gate. The dashboard is unaffected (it sends credentials);
scripts calling these routes need `X-Admin-Key`, a trusted forward-auth identity,
or a loopback connection.

## Provider setup and configuration

The provider settings page now uses the multi-account provider grid by default.
The former single-account overview and edit dialog have been retired. Existing
provider configuration remains available in the grid, including the `default`
account.

Provider configuration updates now require an explicit account ID. Update
scripts and integrations that used:

```http
PUT /api/v1/system/provider-config/openrouter
```

to include the account ID:

```http
PUT /api/v1/system/provider-config/openrouter/default
```

For named accounts, use their stable account ID in the final path segment,
for example `/provider-config/anthropic/alice@example.com`. URL-encode path
segments in clients. The old one-segment `PUT /provider-config/{provider_id}`
shortcut is removed; all writes must identify the account being changed.

The per-account API also avoids ambiguity for providers with multiple saved
credentials. A first credential normally belongs to `default`; use the
provider's account ID for credentials already associated with an identity.

For Kimi API, Kimi K2, MiniMax, OpenRouter, and zAI API keys, lookup precedence
is the saved account credential, then that account's token-cache entry, then
the server environment variable. A cached `default` account key therefore
takes precedence over the corresponding environment variable.

## Docker: the published port now actually works

`docker-compose.yml` and `docker-compose.traefik.yml` now force
`APP_HOST=0.0.0.0` inside the container via Compose's `environment:` block,
overriding whatever `.env` sets. Previously, a plain `cp .env.example .env`
left `APP_HOST=127.0.0.1` — a value Docker's port-forwarding can never
reach, since it lands on the container's own network interface, not its
loopback. The container's healthcheck (which runs *inside* the container
namespace, where loopback works fine) kept passing throughout, so this
silently produced a dead published port rather than a visible failure.

Since the container is now genuinely reachable off-loopback from the app's
own point of view, `DB_ENCRYPTION_KEY`, `TLS_TERMINATED`, `CORS_ORIGINS`,
and (per the multi-host startup gate — see
[SECURITY.md → Multi-Host Startup Gates](SECURITY.md)) `ADMIN_API_KEY` or
`TRUSTED_PROXY_IPS` become mandatory in `.env` before the container will
start at all — see the updated quick-start comment in `docker-compose.yml`. If you were already
setting these (e.g. following `docs/deployment.md`), nothing changes for
you; if you were relying on the old dead-port behavior for some reason,
the container will now refuse to start until you set them.

`make run` also now sources `.env` the same way `make dev` does — some
settings (`CORS_ORIGINS`, and any provider credential configured only via
a registry "env" rule) are read straight from the process environment
rather than through pydantic's own `.env` loading, so a bare
`python -m app.main` or the previous `make run` silently missed them.
