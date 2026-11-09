# Physical security acceptance

The runbook for the checks automation cannot make: native installation, update,
restart and rollback on real Windows and macOS machines, browser boundaries on
real browsers, and a real identity provider in front of Runway. Automated
browser/proxy checks and the frozen verifier ([`../security-validation.md`](../security-validation.md))
complement these; they do not replace them. Record failures and unavailable
cases honestly: a blank result is pending, not passing. Completed runs are
filed under [`../evidence/security/`](../evidence/README.md).

## Record and isolate

Use Windows and Apple Silicon macOS with temporary config dirs, disposable browser
profiles, and synthetic keys. Record OS build, CPU architecture, browser version,
Runway commit/version, payload SHA-256, Sigstore verification result, UTC timestamp,
operator, expected/actual outcome, logs, and a result of pass/fail/blocked. Capture
an initial signed revision A and a later signed revision B as immutable local asset
sets; rolling edge URLs alone are insufficient evidence. Verify each payload and
installer against its bundle and the exact main signing workflow. Keep real fleet
keys, session cookies, Authentik tokens, usernames and provider credentials out of
logs, HARs, screenshots and committed evidence.

Before a test, capture legitimate dashboard settings and sidecar server URL using
their ordinary authenticated interfaces. After every refusal, compare these values
and check that the original installed process can still start. A blank result is
pending, not passing. Do not reuse production config directories or enrolled fleet
identities. Restore temporary DNS, proxy and login-item settings at teardown.

## Native installation matrix

Run each row on both supported installation forms:

| Host | Forms | Acceptance sequence |
| --- | --- | --- |
| Windows x64 | portable ZIP; per-user setup.exe | Install A → launch → update to signed B → restart → rollback to A → relaunch → update to B again |
| macOS Apple Silicon | portable ZIP; DMG dragged to Applications | Install A → launch → update to signed B → restart → rollback to A → relaunch → update to B again |

On each host, also simulate an interrupted update in the isolated test config:
leave an orphaned `self-update.lock` older than `_LOCK_STALE_SECONDS` with no
updater process running, then retry a refused update. Confirm stale-lock reclaim,
unchanged installed/rollback copies, and final lock release. Repeat with a fresh
lock held by another updater; the second update must refuse without deleting it.
Record failed swap/rollback behavior and that the surviving installed copy still
relaunches; retain the error and filesystem evidence rather than calling a
partial rollback successful.

At each step record the running version and executable/bundle location, confirm
configuration survives, confirm only the intended sidecar process runs, and confirm
updates do not leave a stale lock. On macOS also record quarantine/Gatekeeper
prompts, execute permissions, menu-bar launch, and refusal to update directly from
a mounted DMG. On Windows check installer launch, Start Menu launch, and the delayed
swap helper after the old process exits. Sigstore is supply-chain verification;
it does not imply Apple notarization or Windows Authenticode certification.

Run the separate native refusal probe against a **copy** of the installed executable
or app bundle using the same source revision as B:

```bash
python -m scripts.security_native_update_probe --installed-copy <copy-path> \
  --payload <signed-portable-archive> --bundle <matching-sigstore.json> \
  --output <evidence.json>
```

The test driver patches release lookup and transport in its own process only;
production download, checksum, signature and refusal logic remain active. It does
not launch or mutate the real installation. It tests missing bundle, malformed
bundle, tampered payload and oversized response; compare installed/rollback hashes
before and after. Record this evidence separately from the real published-channel
update/restart sequence above. Use Python 3.12 and the repository's sidecar extra.

## Physical browser boundaries

The [Windows helper guide](../evidence/windows-browser-helper.md) describes isolated trusted
TLS direct probes, separate matching-port HTTP rebinding fixtures, scoped real
DNS resolver setup, body-free exports, source restrictions and teardown. Helper
readiness and DNS diagnostics are not physical browser acceptance.

Use Edge/Chrome on Windows, and Safari, Chrome and Firefox on macOS.

Windows Firefox is out of scope: record it as excluded, never as passed. Record
macOS browser availability separately.
Test dashboard and sidecar settings independently. Record Local Network Access
prompts and permission denied/granted states where the browser supports them.

1. Start isolated dashboard/sidecar loopback services with the disposable fixture
   fleet key `synthetic-fleet-key`. The probe's `keySeen` field searches only for
   that exact fixture marker; never use it against production. Confirm the fixture
   value through owner access to the isolated config. Ordinary authenticated
   settings HTML must omit it (the password field stays blank). Validate the
   substring detector separately with a synthetic sample containing the marker;
   do not treat ordinary settings HTML as a positive disclosure control.
2. On a **separate helper host** on an isolated network, run the hostile page and
   controlled DNS responder. Use a hostname under your control and a fresh hostname
   for each run; no public rebinding service is required.

   ```bash
   python -m scripts.security_browser_probe --hostname <controlled-test-host> \
     --helper-ip <helper-LAN-IPv4> --bind <helper-LAN-IPv4> \
     --http-port 8765 --dns-port 53 --target http://127.0.0.1:8765
   ```

   The helper HTTP port must equal the target loopback service's port for same-origin
   rebinding. Repeat with the sidecar's actual settings port. Port 53 needs ordinary
   host permission; default 5353 is only for explicit resolver diagnostics. Configure
   the isolated test network/client resolver to query this fixture for that hostname.
   It answers only that name, with TTL zero, and provides no general DNS recursion.
3. Open the page while DNS answers the helper IP. Attempt hostile-origin writes.
   Record browser network/console results and Local Network Access prompts. The
   page tests dashboard mutation, a simple sidecar form POST, and readable HTML;
   it records only whether a synthetic key was visible, never the response contents.
4. Press the rebinding button: the DNS fixture now answers 127.0.0.1. It forces new
   HTTP connections and retries same-origin requests. Save DNS logs showing the
   second answer and server logs showing rejected hostile Host headers. If the
   browser retains its pinned address or blocks the request first, record a
   **browser-blocked** result; do not claim server rejection was exercised.
5. Compare legitimate dashboard and sidecar settings to the baseline and confirm
   no synthetic fleet key was disclosed. Repeat with LNA permission granted where
   supported so browser denial does not hide the application boundary. Verify
   ordinary same-origin settings use still works.

## Actual Authentik + Traefik over TLS

The completed run is the [Authentik/Traefik execution record](../evidence/security/603-authentik-traefik-2026-10-12/README.md):
an isolated synthetic application with sanitized configuration, results,
immediate-peer logs and teardown.

Create a separate Runway test hostname, Authentik application and Proxy Provider
in forward-auth single-application mode, reusing your existing Traefik installation.
Record component versions and sanitized configuration. Follow the [Authentik
Traefik template](https://docs.goauthentik.io/add-secure-apps/providers/proxy/server_traefik/)
with an outpost route and explicit username/email/groups response headers. Strip
client identity headers **before** forwardAuth; trust only the immediate Traefik
socket peer in Runway. Keep uvicorn proxy-header rewriting disabled. Configure the
specific HTTPS origin and TLS termination flag. Use dedicated allowed/disallowed
users and groups; record the actual source IP seen by Runway.

| Case | Expected result |
| --- | --- |
| Unauthenticated browser | Authentik challenge; private Runway state unavailable |
| Allowed SSO user/group | Private reads and a reversible `PUT /api/v1/system/dashboard-layout` succeed; audit attribution matches synthetic user |
| Disallowed user/group without other credentials | Private operations denied |
| Forged Authentik/Remote-User headers from client | No impersonation through Traefik |
| Direct backend forged identity and X-Forwarded-For | Denied; untrusted immediate peer remains untrusted |
| Admin break-glass key | Works on the isolated app even for a disallowed SSO identity; proxy routing must permit the intended fallback path |
| Valid HMAC sidecar request without SSO cookie | Succeeds on the specifically scoped sidecar route |
| Bad HMAC; repeated valid signature | Rejected; unchanged ingestion state |
| Sidecar API Host/Origin manipulation | Rejected without leaking credentials |

Do not place sidecar routes behind an interactive login challenge; retain HMAC as
the gate and preserve the signature headers/body through Traefik. Do not bypass
SSO for general admin/private routes. Test the configured policy using the real
outpost, not a proxy that asserts a hardcoded test identity.

## Recording results

Attach sanitized results and logs to the validation record, and file newly
found defects as issues with reproductions. A run is complete only when all
required native, browser and SSO cases pass or a limitation is explicitly
agreed and tracked. Suggested record shape:

```json
{"case":"windows-installer-update","status":"pending","expected":"signed B starts; config preserved","actual":null,"os":null,"browser":null,"commit":null,"asset_sha256":null,"operator":null,"tested_at":null,"evidence":[]}
```

## Release acceptance

Image promotion and sidecar signing are separate acceptance boundaries. An image
scan must describe the exact published digest, and a frozen verifier must accept
the actual captured portable release bytes, not only a synthetic signed fixture.
Neither replaces the physical checks above.

### Portable release capture and frozen validation

Dispatch **Frozen Verifier** on the trusted source branch with `release-tag` set
to a versioned release such as `v3.1.0`; rolling `edge` is deliberately
unsupported. The capture job requires Linux CLI, Linux desktop, Windows ZIP and
macOS ZIP, each with a checksum and modern Sigstore bundle. It records the
release ID, tag commit, asset IDs, sizes, GitHub digests when supplied and
locally computed SHA-256 hashes, checks checksums, runs the production updater
verifier on all four payloads, then rechecks tag and asset identity for
replacement during capture.

The native frozen matrix uses that single asset set and invokes the normal
`--verify-update` diagnostic in each frozen binary with an isolated config
directory. A valid archive must pass; tampered bytes, missing or malformed
bundles and oversized archives must refuse with the expected exit code and
diagnostic. Nothing is installed or extracted. Sigstore trust refresh needs
network access, and a failure never permits a checksum fallback or a weakened
signing identity.

`captured-release-updates` and `frozen-verifier-*` artifacts retain manifests and
results for 90 days; keep a durable copy if the evidence is needed longer. A
successful run, the source commit and the capture manifest hashes are the record.
Locally:

```bash
python -m scripts.capture_release_updates --release-tag v3.1.0 --output /tmp/release-capture
python -m scripts.security_packaging_smoke --binary dist/runway-sidecar-cli \
  --release-dir /tmp/release-capture --platform Linux-CLI --report /tmp/linux-cli.json
```

Without release inputs, PR checks use the real signed fixture. An explicit
`--payload`/`--bundle` pair is also supported for packaging diagnostics.

### Published image

Record the published index and both architecture digests, raw SBOM/report
artifacts, database identity and scan time, the exact reviewed exceptions, and
the signing and promotion steps. Independently verify the public index signature
against the Docker publishing workflow identity (see [`../SECURITY.md`](../SECURITY.md))
and confirm the public versioned tag resolves to the audited index.
Source-only or candidate-only validation does not establish replacement of
released bytes. The recorded scan is in [`image-remediation.md`](image-remediation.md).
