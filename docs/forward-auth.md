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

## Notes

- The admin key (`ADMIN_API_KEY`) keeps working as a fallback — `X-Admin-Key` for scripts, or the existing session-cookie login screen if you ever hit Runway directly (bypassing the proxy) and need to recover access.
- Sign-out is Authentik's job once SSO is active — see the Settings → System → Session card, which explains this when `user_context` is set.
- The IP allowlist (`TRUSTED_PROXY_IPS`) is the real security boundary here, not the header names — never point it at an IP range you don't fully control, since anything in that range can forge the identity headers directly.
