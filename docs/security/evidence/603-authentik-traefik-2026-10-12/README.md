# Actual Authentik + Traefik TLS validation — 2026-10-12

Issue: [#603](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/603).
Runbook/tooling: [#608](https://github.com/s3ntin3l8/runway-ai-usage-tracker/pull/608)
and [physical-validation.md](../../physical-validation.md).

**Result: all 51 checks passed for the actual Authentik/Traefik TLS matrix.**
This is an isolated application using the existing real Authentik core, dev-01
outpost and Traefik installation. It certifies the recorded test configuration;
it does not certify production users/policies or native installation and physical
browser Local Network Access/DNS rebinding behavior. #603 remains open.

## Versions and isolation

Operator: Codex acting for the repository owner on dev-01. Ubuntu 24.04.5 LTS,
x86_64, kernel `7.0.14-3-pve`; headless Chromium `156.0.8078.4` with disposable,
independent browser contexts and normal certificate verification enabled.
This Linux browser was used for SSO acceptance only.

Runway `3.0.0-beta.9`, source revision
`70779c444da05c9e19aad0b5a87f4979d42ab081`, using the already deployed immutable
image `ghcr.io/s3ntin3l8/runway@sha256:d54325987b93950279d6373c17c85e6fc6d1d617286f2b120389da31647a3c54`.
Authentik core/outpost `2026.8.3`; Traefik `v3.7.1`; Python `3.12.15`, uvicorn
`0.54.0`, FastAPI `0.143.0`, Pydantic `2.14.0`. The HTTPS connection negotiated
TLS 1.3 with a verified Let's Encrypt certificate. Exact UTC timestamps and TLS
details are in [results.json](results.json).

The reference deployment is `https://runway.test.local/`, and the test
application used `https://runway-603.test.local`. The test container
used a new `runway-603-data` volume with `/data/runway.db`, generated synthetic
admin/ingest/encryption keys and one synthetic provider credential. Production
Runway's config/database and fleet identities were never mounted in it.

The full Runway image/API was used. A small ASGI wrapper called `init_db` and
logged only method, path, socket peer, Host, Origin, X-Forwarded-For and response
status. Uvicorn ran with `--no-proxy-headers --lifespan off`; disabling lifespan
prevented collectors and unrelated background jobs, without replacing endpoint,
authentication, ingestion or replay logic.

## Configuration and method

- A separate Authentik application/Proxy Provider used `forward_single` mode,
  the exact test HTTPS origin, the ordinary implicit-consent authorization and
  provider-invalidation flows, and the real dev-01 outpost. Header interception
  was disabled. A dedicated password/login flow was limited to synthetic users;
  production authentication flows were unchanged.
- `runway-603-app-access` admitted the allowed and Runway-disallowed users at the
  Authentik layer. Runway's allowed-group list contained only
  `runway-603-allowed`, so the second user reached Runway but was refused there.
  A third synthetic user outside app-access exercised Authentik's own denial.
- [traefik-test.yml](traefik-test.yml) records the actual temporary routing.
  Identity stripping precedes real forwardAuth, which explicitly forwards the
  outpost's username/email/groups. Authentik, Remote-User and legacy forwarded
  identity headers are also stripped on HMAC and recovery routes. Only the exact
  ingest/config/credential-manifest paths bypass interactive SSO.
- Runway trusted only the pinned immediate Traefik address `172.20.0.254`.
  `APP_HOST=0.0.0.0`, the exact `CORS_ORIGINS`/`PUBLIC_URL` and
  `TLS_TERMINATED=1` were configured. Synthetic secrets are omitted from evidence.
- Recovery used a **test-only** `/validation-break-glass` prefix mapped only to
  the dashboard-layout endpoint, restricted to operator source `192.0.2.10`.
  Identity headers were stripped; Runway still required the admin key. Other
  private routes retained SSO. This extra route was not added to production.
- Real browser username/password logins established independent SSO sessions.
  Allowed layout writes used browser `fetch`, including the real HTTPS Origin.
  Writes were read back and reversed; refusals compared the isolated business
  state before/after, including replay receipts and sidecar registry state.
- HMAC used the exact transmitted body bytes (`timestamp + body`), and config
  signatures bound the raw query (`timestamp + "GET:" + query`). No SSO cookie
  or admin key was present for sidecar requests. Replays repeated the original
  signature and timestamp. The synthetic credential positive control was
  inserted into the isolated authoritative provider config, with collectors off.

## Outcomes

| Runbook case | Actual outcome |
| --- | --- |
| Unauthenticated browser/private API | Dashboard opened the real Authentik login form; private API returned 302 to Authentik, without private state. |
| Allowed user/group | Dashboard and private reads returned 200; browser PUT persisted the synthetic layout and restoration succeeded. |
| Disallowed group without other credentials | Runway returned 403 for reads/writes; state remained unchanged. The separate IdP-denied user reached Authentik's permission-denied page. |
| Forged headers through Traefik | The real allowed identity remained authoritative; a disallowed user could not forge membership. Unauthenticated forging still challenged. |
| Direct backend forged identity/XFF | GET/PUT returned 403; state remained unchanged. Recorded immediate peer was `172.20.0.1`, despite trusted-proxy XFF `172.20.0.254`. |
| Immediate peer/audit attribution | SSO mutation audits attributed `runway-603-allowed` and recorded `172.20.0.254`, including the forged-forwarding-header write. |
| Admin break-glass | Correct key succeeded even with the disallowed user's SSO cookies; wrong/missing keys and forged identities failed. Reversible writes worked without SSO; hostile Origin and non-operator source failed. |
| HMAC without SSO | Ingest returned 200 and inserted one synthetic usage event and one sidecar registry row. Signed config and credential manifest returned 200. |
| Bad HMAC/body/query tampering | 401; unchanged ingestion/config state. Missing ingest/manifest HMAC returned 401 rather than an interactive login challenge. |
| Replay | Ingest, signed config and credential manifest returned 409; persisted state was unchanged. |
| Host/Origin manipulation | Host mismatch returned Traefik 404; hostile Origin returned Runway 403. All three sidecar paths rejected manipulation with unchanged state. Ingest/config responses were explicitly checked for fixture-secret/token disclosure; none was found. |
| Credential disclosure positive control | Signed config issued an opaque credential token for the synthetic account. Unsigned config omitted account ids/tokens; manipulated Host/Origin responses did not expose them. |

Authentik's denial is not always an HTTP 403 at the original private GET: the
outpost redirects to Authentik, whose permission-denied HTML returned 200.
The observed denial text and final host were checked; no private Runway data was
accepted as evidence of authentication. The original direct-status assumption
was corrected to describe this actual policy behavior.

One driver preflight used `timestamp` instead of the event schema's `ts` field.
Runway correctly refused it with 400; replay of that rejected signed envelope
also refused with 409. The driver was corrected and the valid event was accepted.
This is retained in `driver_preflight` in results.json and the peer log; it is a
fixture error, not a failed production security boundary.

[results.json](results.json) contains all expected/actual outcomes and synthetic
audit attribution. [request-peers.jsonl](request-peers.jsonl) contains the
whitelisted backend request observations. It omits session cookies, bearer/API
tokens, signatures, request/response bodies and real user/provider identities.

## Teardown and remaining acceptance

Teardown completed and was verified at the UTC time in `results.json`:

- Removed the test provider from the existing outpost; its original provider set
  was restored, preserving any concurrent assignments.
- Removed the temporary Traefik file, test container and database volume, and all
  17 synthetic Authentik objects (users, groups, application/provider, flow,
  stages and bindings). Verification found no remaining test objects.
- An initial cleanup lookup used a flow UUID where the API requires its slug.
  Verification caught the remaining flow; the corrected lookup removed it before
  completion. The token's identifier also differed from the suggested label;
  revocation proceeded only after an exact token-value match.
- Revoked the supplied management token, verified its next API request returned
  403, and removed its local file plus synthetic secrets, SSO cookies and full
  API snapshots. Browser contexts/processes were closed. No DNS changes were
  made; the shared existing TLS certificate was retained.
- Verified the original Runway provider settings were unchanged, production
  Runway remained healthy and its private API still challenged with 302, and
  the removed test hostname returned Traefik 404.

The full repository gate passed: ruff lint/format, mypy and dependency audit;
4,319 Python tests passed (36 skipped), and 1,155 frontend tests passed.

Windows ZIP/setup.exe and Apple Silicon ZIP/DMG install/update/restart/rollback,
and physical browser LNA/DNS rebinding acceptance remain pending in #603. This
run adds no claim of those cases passing and does not close the issue.
