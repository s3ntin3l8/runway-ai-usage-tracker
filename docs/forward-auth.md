# Forward-Auth / SSO (Authentik walkthrough)

Runway has no built-in OIDC client. It trusts an identity header asserted by a forward-auth proxy in front of it, which is the recommended pattern for a multi-host or Docker deployment: your identity provider (Authentik, Authelia, oauth2-proxy, Cloudflare Access, Tailscale, …) authenticates, and Runway consumes the result. This guide walks through Authentik so the dashboard never prompts for the admin key. The trust model is in [SECURITY.md](SECURITY.md#authentication).

Other providers: oauth2-proxy and Authelia inject `X-Forwarded-User` (and optionally `X-Forwarded-Email` / `X-Forwarded-Groups`), which already match the defaults of `FORWARD_AUTH_USER_HEADER`, `FORWARD_AUTH_EMAIL_HEADER` and `FORWARD_AUTH_GROUPS_HEADER`. Cloudflare Access and Tailscale terminate identity at the edge; forward the asserted user header from a fixed proxy IP listed in `TRUSTED_PROXY_IPS`. A CGI-style `Remote-User` header is accepted as a fallback only while `FORWARD_AUTH_USER_HEADER` is left at its default.

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

> **Spoofing risk:** the middleware above only *adds* the outpost's headers on top of whatever the client sent; it does not strip a client-supplied `X-authentik-username`. `TRUSTED_PROXY_IPS` is the real boundary (Runway reads these headers only from that source IP), but anything else on the Docker network that can reach the `runway` container directly could forge them. Production setups chain a header-stripping step before `forwardAuth`. §5 uses such a chain, `chain-authentik@file`; it is a name you define yourself in Traefik's file provider, for example:
>
> ```yaml
> http:
>   middlewares:
>     strip-identity:
>       headers:
>         customRequestHeaders:
>           X-authentik-username: ""
>           X-authentik-email: ""
>           X-authentik-groups: ""
>     chain-authentik:
>       chain:
>         middlewares: [strip-identity, authentik@docker]
> ```
>
> Defining the chain once in shared config also means a later security fix is not repeated per app.

## 3. Runway: trust the proxy and read Authentik's headers

```bash
# Runway env (composes with the startup gates in SECURITY.md)
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
# Genuinely optional here only because TRUSTED_PROXY_IPS is set above — on a
# non-loopback bind, the startup gate requires at least one of the two (see
# SECURITY.md → Multi-Host Startup Gates).
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

Gating the *entire* host behind `chain-authentik@file` (or whatever your forwardAuth middleware is named) breaks requests that don't authenticate as a browser:

- **Sidecar requests.** Five fleet routes are called by sidecars that have no browser session, so none of them can answer a proxy's login redirect:

  | Route | How Runway authenticates it | If the proxy redirects it instead |
  | --- | --- | --- |
  | `POST /api/v1/fleet/ingest` | sidecar HMAC (legacy ingest key or enrolled device key) | metrics and events stop arriving |
  | `GET /api/v1/fleet/config` | HMAC for the full view; unsigned callers get a limited one | account mappings never reach the sidecar |
  | `POST /api/v1/fleet/credentials/manifest` | sidecar HMAC | discovered credentials cannot be assigned, even though ingest still works |
  | `POST /api/v1/fleet/source-sync` | enrolled **device** key | a paired Mac never completes a source sync, so it keeps its Keychain-derived credentials (for example the Claude Console cookie) to itself, and pause/revoke commands never reach any enrolled host |
  | `POST /api/v1/fleet/pair` | the one-time pairing code itself, rate limited to 10/min/IP | a sidecar cannot pair |

  Runway applies its own request checks to every one of them, so bypassing proxy SSO does not bypass endpoint authentication. The failure is quiet (ingest keeps working while `source-sync` is redirected), so check all five (see *Verify the bypass* below).
- **Performance.** The content-hashed bundle under `/assets/*` is a couple dozen requests per page load, each of which would pay the forwardAuth round trip (see §6).
- **The PWA's update mechanism.** The browser treats a redirect on its periodic `/sw.js` fetch as a failed update. If `/sw.js` is SSO-gated, a client whose Authentik session lapses can never fetch a newer service worker and keeps replaying the build it cached. `/manifest.webmanifest` and the install and splash icons need the same bypass.

Fix: four Traefik routers on the same `runway` service, differentiated by path and priority — only the default (Host-only) router carries the SSO middleware:

```yaml
services:
  runway:
    labels:
      - traefik.enable=true
      - traefik.http.services.runway.loadbalancer.server.port=8765

      # Sidecar endpoints bypass SSO — each request has its own HMAC auth (the
      # pairing route authenticates by its one-time code instead).
      # Runway applies its own request checks to every listed route.
      # Keep this exact list aligned with the HMAC-only fleet routes in the
      # app/api/endpoints/fleet_*.py modules
      # plus the code-authenticated pairing route; tests/unit/test_forward_auth_routes.py
      # checks route and doc parity. Newly added routes stay behind Authentik
      # until explicitly listed here.
      - "traefik.http.routers.runway-ingest.rule=Host(`runway.example.com`) && (Path(`/api/v1/fleet/ingest`) || Path(`/api/v1/fleet/config`) || Path(`/api/v1/fleet/credentials/manifest`) || Path(`/api/v1/fleet/source-sync`) || Path(`/api/v1/fleet/pair`))"
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

      # PWA lifecycle files bypass SSO too — see explanation above. Without
      # this, a client whose SSO session lapses can never receive an updated
      # service worker again.
      - "traefik.http.routers.runway-pwa.rule=Host(`runway.example.com`) && (Path(`/sw.js`) || Path(`/manifest.webmanifest`) || PathRegexp(`^/(pwa-|apple-|maskable-|favicon).*`))"
      - traefik.http.routers.runway-pwa.entrypoints=websecure
      - traefik.http.routers.runway-pwa.tls.certresolver=le
      - traefik.http.routers.runway-pwa.priority=100
      - traefik.http.routers.runway-pwa.service=runway

      # Everything else — the dashboard + admin API — stays SSO-gated.
      - "traefik.http.routers.runway.rule=Host(`runway.example.com`)"
      - traefik.http.routers.runway.entrypoints=websecure
      - traefik.http.routers.runway.tls.certresolver=le
      - traefik.http.routers.runway.middlewares=chain-authentik@file
```

`.service=runway` on the extra routers is required — Traefik otherwise assumes a service named after the router (`runway-ingest`, which doesn't exist). The explicit `priority=100` guarantees the path-scoped routers win over the Host-only one regardless of how Traefik's default rule-length priority happens to compute; don't rely on the default.

### Authentik: unauthenticated paths instead of router splitting

If you would rather keep one Traefik router and let Authentik skip the sidecar routes, set the proxy provider's **Unauthenticated URLs** (Provider -> *Advanced protocol settings*; `skip_path_regex` in the API and in Terraform's `authentik_provider_proxy`). One anchored regex per line, the same five routes plus the static and PWA files from above:

```text
^/api/v1/fleet/ingest/?$
^/api/v1/fleet/config/?$
^/api/v1/fleet/credentials/manifest/?$
^/api/v1/fleet/source-sync/?$
^/api/v1/fleet/pair/?$
^/sw\.js$
^/manifest\.webmanifest$
^/(pwa-|apple-|maskable-|favicon).*
^/assets/.*
```

Add `^/api/v1/system/health$` as well if a monitor probes Runway's health route. Use one mechanism or the other, not a mix that drifts: whichever you choose, the list has to match the table above.

### Verify the bypass

A blocked path fails quietly, so probe each one from outside, without a session and without following redirects. A path that reaches Runway answers `401` or `422` (the signature-protected `POST` routes), or `200` for `GET config`, which gives an unsigned caller the limited view; a `302` whose `redirect_url` points at your identity provider means the proxy is still gating it:

```bash
H=https://runway.example.com
for p in ingest credentials/manifest source-sync pair; do
  printf '%-22s ' "POST $p"
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d '{}' \
       -w '%{http_code} %{redirect_url}\n' "$H/api/v1/fleet/$p"
done
printf '%-22s ' "GET config"; curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$H/api/v1/fleet/config"
```

If `source-sync` shows a redirect, a paired Mac has no source states on the server (Fleet's *Sources* view for it stays empty), its Keychain-derived credentials are withheld, and the server log never sees a `source-sync` request.

## 6. Why this matters: forward-auth latency

Every request through `chain-authentik@file` costs a round trip from the outpost to Authentik's core server, typically 50-150 ms when the two are on different hosts. A server-rendered page pays that once; Runway is a SPA whose page view fires dozens of requests (JS/CSS/font chunks plus several API calls) and keeps polling, so without the router split each one pays it. The `runway-static` bypass removes the multiplier for the bulk of those requests. To check whether this is your bottleneck, compare an SSO-gated path with a bypassed one on the same container:

```bash
curl -s -o /dev/null -w "%{time_total}s\n" -H "Host: runway.example.com" https://<traefik-host>/           # SSO-gated
curl -s -o /dev/null -w "%{time_total}s\n" -H "Host: runway.example.com" -X POST https://<traefik-host>/api/v1/fleet/ingest  # bypassed
```

A large gap points at the forwardAuth round trip, not Runway.

## 7. Stabilizing `TRUSTED_PROXY_IPS`

`TRUSTED_PROXY_IPS` is an **exact IP match, not a CIDR** (see [SECURITY.md](SECURITY.md#authentication)). If your reverse proxy's container IP isn't pinned on its Docker network, it's assigned in creation order and can drift the next time that container is recreated — silently breaking forward-auth trust (Runway falls back to the admin key / a 403, rather than failing open).

Fix: pin a static IP for the proxy service on its network. This requires the network to already have a defined subnet (check with `docker network inspect <network> --format '{{json .IPAM}}'` — if it shows a `Subnet`, you're set):

```yaml
services:
  traefik:            # or nginx / caddy / whatever fronts Runway
    networks:
      proxy:
        ipv4_address: 10.0.0.2   # match this to TRUSTED_PROXY_IPS
```

Apply with `docker compose up -d` in that stack's directory. This recreates the proxy container, so every app it proxies sees a brief blip. Confirm afterward:

```bash
docker inspect <proxy-container> --format '{{.NetworkSettings.Networks.<network>.IPAddress}}'
```

## Notes

- `ADMIN_API_KEY` keeps working as a fallback: `X-Admin-Key` for scripts, or the key-entry screen if you reach Runway directly and need to recover access.
- Sign-out is the identity provider's job once SSO is active; Settings → System → Session says so when a proxy user is present.
- Never point `TRUSTED_PROXY_IPS` at a range you do not fully control: anything in it can forge the identity headers.
