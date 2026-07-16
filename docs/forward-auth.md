# Forward-Auth / SSO (Authentik walkthrough)

Runway doesn't ship a built-in OIDC client (see `docs/SECURITY.md` → *Decision (issue #92)*) — instead it trusts an identity header asserted by a forward-auth proxy sitting in front of it. This is the recommended pattern for a multi-host/Docker deployment: your identity provider (Authentik, Authelia, oauth2-proxy, Cloudflare Access, Tailscale, …) does real authentication, and Runway just consumes the result. This doc walks through the concrete Authentik setup so the dashboard never prompts for the admin key again.

See `docs/SECURITY.md` → *Application Authentication* for the full trust-ladder reference; this doc is the practical how-to.

## 1. Authentik: Proxy Provider + Outpost

1. In Authentik, create a **Provider** → *Proxy Provider*, mode **"Forward auth (single application)"**, pointed at Runway's external URL (e.g. `https://runway.example.com`).
2. Create an **Application** bound to that provider, and assign it to the group(s) that should be allowed to reach Runway (e.g. `runway-admins`). This is Authentik's own authorization gate — it decides who ever reaches the outpost at all.
3. Attach the provider to your outpost (the embedded outpost works fine for a single app).

Authentik's outpost asserts identity via headers on the request it forwards through: `X-authentik-username`, `X-authentik-email`, `X-authentik-groups` (pipe-`|`-delimited), plus a few others Runway doesn't use.

## 2. Reverse proxy: wire forward-auth to the outpost

Using Traefik (see `docker-compose.traefik.yml` for the base stack), add a `forwardAuth` middleware pointed at the Authentik outpost and apply it to the Runway router:

```yaml
services:
  runway:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.runway.rule=Host(`runway.example.com`)"
      - "traefik.http.routers.runway.entrypoints=websecure"
      - "traefik.http.routers.runway.tls.certresolver=le"
      - "traefik.http.routers.runway.middlewares=authentik@docker"
      - "traefik.http.services.runway.loadbalancer.server.port=8765"
      # The forward-auth middleware itself, pointed at the Authentik outpost:
      - "traefik.http.middlewares.authentik.forwardauth.address=http://authentik-outpost:9000/outpost.goauthentik.io/auth/traefik"
      - "traefik.http.middlewares.authentik.forwardauth.trustForwardHeader=true"
      - "traefik.http.middlewares.authentik.forwardauth.authResponseHeaders=X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-uid,X-authentik-name"
```

`authResponseHeaders` is what makes the outpost's identity headers actually reach the `runway` container — without it Traefik strips them. Other proxies (nginx `auth_request`, Caddy `forward_auth`) follow the same shape: authenticate against the outpost, then forward its response headers through to the upstream.

> **Spoofing risk:** the middleware above only *adds* the outpost's headers on top of whatever the client sent — it doesn't strip a client-supplied `X-authentik-username` first. Since `TRUSTED_PROXY_IPS` is the actual security boundary (Runway only reads these headers from a request whose source IP is in that allow-list), a request that *never reaches Traefik's public entrypoint at all* isn't a risk — but if anything else on your Docker network could reach the `runway` container directly, it could forge these headers. Production setups should chain a header-stripping step before `forwardAuth` (blank the identity headers, then let the outpost set the real ones) — see §5, which references this as `chain-authentik@file` instead of the bare `authentik@docker` above.

## 3. Runway: trust the proxy and read Authentik's headers

```bash
# Runway env (composes with the multi-host gate — see docs/SECURITY.md)
APP_HOST=0.0.0.0
TLS_TERMINATED=1
DB_ENCRYPTION_KEY=<fernet-key>
CORS_ORIGINS=https://runway.example.com

# Trust only Traefik's container/network IP — never a range you don't control.
TRUSTED_PROXY_IPS=10.0.0.2

# Authentik's native outpost headers — no proxy-side renaming needed.
FORWARD_AUTH_USER_HEADER=X-authentik-username
FORWARD_AUTH_EMAIL_HEADER=X-authentik-email
FORWARD_AUTH_GROUPS_HEADER=X-authentik-groups

# Optional: restrict which asserted users/groups actually get admin access,
# on top of Authentik's own application binding.
FORWARD_AUTH_ALLOWED_GROUPS=runway-admins

# Optional but recommended: a break-glass key for scripts, or for recovery if
# Authentik is ever unreachable. Never entered in the browser once SSO works.
ADMIN_API_KEY=<strong secret>
```

Restart Runway. Visiting the dashboard through the proxy now authenticates automatically — `BootGate` never renders the key-entry screen, and Settings → About shows `Auth methods: Forward Auth (SSO)` and `User: <your Authentik username>`.

## 4. Verifying it works

```bash
# Through the proxy, as a real browser session would see it:
curl -s https://runway.example.com/api/v1/system/settings | jq '.is_authenticated, .user_context, .auth_methods'
# → true, "your-username", ["admin_key", "forward_auth"]
```

If `is_authenticated` is `false`:
- Confirm `TRUSTED_PROXY_IPS` matches the actual source IP Runway sees for proxied requests (log it if unsure — a Docker network IP, not the proxy's public IP).
- Confirm the outpost's `authResponseHeaders` (or nginx/Caddy equivalent) actually forwards the three headers — inspect them with `curl -v` against the outpost directly, or temporarily log request headers server-side.
- If `FORWARD_AUTH_ALLOWED_GROUPS`/`_USERS` is set, confirm your Authentik group name matches exactly (case-sensitive, split on `,` or `|`/whitespace).

## 5. Production hardening: split the Traefik router

Gating the *entire* host behind `chain-authentik@file` (or whatever your forwardAuth middleware is named) breaks two things that don't authenticate as a browser:

- **Sidecar ingestion.** `POST /api/v1/fleet/ingest` authenticates via its own HMAC signing (`INGEST_API_KEY`), not Authentik. A remote sidecar's push would get redirected into the SSO challenge instead of ever reaching Runway.
- **Performance.** Vite's content-hashed JS/CSS/font bundle (`/assets/*`) is a couple dozen separate requests per page load, and each one would separately pay the forwardAuth round-trip cost (see *Why this matters* below).

Fix: three Traefik routers on the same `runway` service, differentiated by path and priority — only the default (Host-only) router carries the SSO middleware:

```yaml
services:
  runway:
    labels:
      - traefik.enable=true
      - traefik.http.services.runway.loadbalancer.server.port=8765

      # Sidecar ingestion bypasses SSO — it has its own HMAC auth.
      - "traefik.http.routers.runway-ingest.rule=Host(`runway.example.com`) && PathPrefix(`/api/v1/fleet/ingest`)"
      - traefik.http.routers.runway-ingest.entrypoints=websecure
      - traefik.http.routers.runway-ingest.tls.certresolver=le
      - traefik.http.routers.runway-ingest.priority=100
      - traefik.http.routers.runway-ingest.service=runway

      # Immutable static assets bypass SSO too — no user data, already
      # served with a far-future Cache-Control by Runway's own
      # _ImmutableAssetsMiddleware, so there's nothing to protect here.
      - "traefik.http.routers.runway-static.rule=Host(`runway.example.com`) && PathPrefix(`/assets/`)"
      - traefik.http.routers.runway-static.entrypoints=websecure
      - traefik.http.routers.runway-static.tls.certresolver=le
      - traefik.http.routers.runway-static.priority=100
      - traefik.http.routers.runway-static.service=runway

      # Everything else — the dashboard + admin API — stays SSO-gated.
      - "traefik.http.routers.runway.rule=Host(`runway.example.com`)"
      - traefik.http.routers.runway.entrypoints=websecure
      - traefik.http.routers.runway.tls.certresolver=le
      - traefik.http.routers.runway.middlewares=chain-authentik@file
```

`.service=runway` on the extra routers is required — Traefik otherwise assumes a service named after the router (`runway-ingest`, which doesn't exist). The explicit `priority=100` guarantees the path-scoped routers win over the Host-only one regardless of how Traefik's default rule-length priority happens to compute; don't rely on the default.

## 6. Why this matters: forward-auth latency

Every request that goes through `chain-authentik@file` costs a real network round-trip — the outpost has to validate the session against Authentik's core server before Traefik forwards the request on. If that core server isn't co-located with the outpost (a very common setup: outpost + Traefik on one host, Authentik itself on another), that round-trip is 50-150ms, not the sub-millisecond you'd see hitting the app directly.

For a traditional server-rendered app — one request per page load — that's invisible. Runway is a SPA: a single page view fires off dozens of requests (every JS/CSS/font chunk, plus several API calls for cards/status/settings), and background polling keeps firing more. Before the router split above, *every one* of those independently paid the same 50-150ms tax, which compounds into very noticeable load times, especially on first load or when a browser tab regains focus (which retriggers a burst of API calls). The `runway-static` bypass removes the multiplier for the bulk of that request count — only the page load and actual API calls still pay the (now singular, not multiplied) cost.

To check whether this is your bottleneck, compare latency through an SSO-gated path vs. an unprotected one on the same container (the ingest router is a convenient control group):

```bash
curl -s -o /dev/null -w "%{time_total}s\n" -H "Host: runway.example.com" https://<traefik-host>/           # SSO-gated
curl -s -o /dev/null -w "%{time_total}s\n" -H "Host: runway.example.com" -X POST https://<traefik-host>/api/v1/fleet/ingest  # bypassed
```

A large gap between the two confirms the forwardAuth round-trip (not Runway itself) is the slow part.

## 7. Stabilizing `TRUSTED_PROXY_IPS`

`TRUSTED_PROXY_IPS` is an **exact IP match, not a CIDR** (see `docs/SECURITY.md`). If your reverse proxy's container IP isn't pinned on its Docker network, it's assigned in creation order and can drift the next time that container is recreated — silently breaking forward-auth trust (Runway falls back to the admin key / a 403, rather than failing open).

Fix: pin a static IP for the proxy service on its network. This requires the network to already have a defined subnet (check with `docker network inspect <network> --format '{{json .IPAM}}'` — if it shows a `Subnet`, you're set):

```yaml
services:
  traefik:            # or nginx / caddy / whatever fronts Runway
    networks:
      proxy:
        ipv4_address: 10.0.0.2   # match this to TRUSTED_PROXY_IPS
```

Apply with `docker compose up -d` in that stack's directory — this recreates just the proxy container, so expect a brief reconnect blip for *every* app it proxies, not just Runway. Confirm afterward:

```bash
docker inspect <proxy-container> --format '{{.NetworkSettings.Networks.<network>.IPAddress}}'
```

## Notes

- The admin key (`ADMIN_API_KEY`) keeps working as a fallback — `X-Admin-Key` for scripts, or the existing session-cookie login screen if you ever hit Runway directly (bypassing the proxy) and need to recover access.
- Sign-out is Authentik's job once SSO is active — see the Settings → System → Session card, which explains this when `user_context` is set.
- The IP allowlist (`TRUSTED_PROXY_IPS`) is the real security boundary here, not the header names — never point it at an IP range you don't fully control, since anything in that range can forge the identity headers directly.
- If you're running multiple apps behind the same forwardAuth-capable proxy, define the middleware once in the proxy's shared/file-provider config rather than per-app docker labels — every app then references it the same way (`chain-authentik@file` in the examples above), so a security fix (like adding an identity-header strip step) never has to be repeated per app.
