# Windows physical browser helper — #603

This prepares the separate helper host. Windows acceptance belongs to the existing
Windows agent/human operator. Keep #603 open, macOS acceptance pending, and #597
open for native tray-control verification. Do not repeat the completed actual
Authentik/Traefik matrix. Follow [physical-validation.md](physical-validation.md).

## Preconditions and isolation

- Confirm the Windows agent's synthetic loopback targets are alive: sidecar
  `http://127.0.0.1:18653`, dashboard `http://127.0.0.1:18655`. Obtain the PC's
  helper-visible IPv4 and OS/browser versions before allowing its traffic.
- Use an explicitly controlled domain and fresh HTTP hostnames for **each target,
  browser and run**. A helper on dev-01 can bind a private LAN address on a
  shared interface, not a separate physical test VLAN. The
  fixture is isolated by process, explicit bind address and client allow-list.
  Use the confirmed PC source IPv4 as a `/32`; the helper's own `/32` may also
  be admitted for controller diagnostics. Membership is checked against the
  connecting peer, so admitting the bind address does not admit other LAN hosts.
  Supply a reserved address/interface instead if VLAN isolation is required.
- Obtain a separate TLS certificate/key for the TLS page hostname. Serve it on
  its own listener, not production Traefik. Verify the ordinary Windows browser
  accepts the chain/name without exceptions. Self-signed helper transport tests
  are not evidence of trusted browser TLS. Keep private keys outside the repo.
  The configured TLS hostname must appear in the certificate SAN; a mismatch
  causes ordinary browser certificate verification to fail and is not acceptance.
  Use the exact `/sidecar` and `/dashboard` URLs; the TLS root `/` returns 404.
- The fixture serves only exact configured names, with no forwarding/recursion,
  A answers with TTL zero, and no AAAA addresses. Other names return NXDOMAIN;
  unallowlisted clients receive no service. DNS supports UDP and TCP on the same
  port. A TLS hostname, when configured, always answers the helper IPv4.

## Start and stop

Use current remote main containing #622 (`85c0dfe3`) plus the reviewed helper
tooling revision. Install dependencies in its dedicated worktree. Substitute the
confirmed client IP, fresh hostnames and protected certificate paths:

```bash
sudo systemd-run --unit=runway603-windows-helper \
  --property=User=<HELPER_LINUX_USER> --property=AmbientCapabilities=CAP_NET_BIND_SERVICE \
  --property=NoNewPrivileges=yes --property=RuntimeMaxSec=4h \
  --working-directory=/absolute/path/to/helper-worktree \
  /absolute/path/to/helper-worktree/.venv/bin/python \
  -m scripts.security_browser_helper \
  --bind <HELPER_LAN_IPV4> --dns-port 53 \
  --allowed-client '<WINDOWS_IPV4>/32' --allowed-client <HELPER_LAN_IPV4>/32 \
  --fixture 'sidecar,<FRESH_SIDECAR_FQDN>,http://127.0.0.1:18653' \
  --fixture 'dashboard,<FRESH_DASHBOARD_FQDN>,http://127.0.0.1:18655' \
  --tls-hostname '<TLS_FQDN>' --https-port 19443 \
  --tls-cert /protected/test/fullchain.pem --tls-key /protected/test/tls.key \
  --evidence-dir /protected/test/run-01
```

`<HELPER_LAN_IPV4>` is the helper host's own address on the LAN interface the
fixture binds to. It must be an address the helper can actually bind; a
documentation-range address such as `192.0.2.0/24` cannot be bound and the
listener fails with `EADDRNOTAVAIL`.

Only the transient test unit receives the low-port capability. Existing resolver,
Runway, Authentik, proxy routes, firewall policy and normal DNS settings are not
reconfigured. Confirm the explicit interface permits the PC's UDP/TCP 53 and TCP
18653, 18655, 19443; do not open these services to other clients.

TLS direct-probe URLs:
`https://<TLS_FQDN>:19443/sidecar` and `/dashboard`.
HTTP rebinding URLs:
`http://<FRESH_SIDECAR_FQDN>:18653/` and
`http://<FRESH_DASHBOARD_FQDN>:18655/`.

```bash
sudo systemctl stop runway603-windows-helper
sudo systemctl reset-failed runway603-windows-helper
```

Stopping writes `helper-stopped` and releases listeners. Generate new HTTP names
and a separate evidence directory before another run; arming is irreversible
within a process. The two fixtures arm independently. The unit expires after
four hours; coordinate its lease and the temporary Windows targets.

## Human-controlled resolver and browser setup

In an elevated Windows PowerShell, first record the existing effective NRPT/DNS
configuration. Route **only these exact test names** to the helper using temporary
NRPT rules; preserve the rule identifiers for removal:

```powershell
$ProbeNames = @('<TLS_FQDN>', '<FRESH_SIDECAR_FQDN>', '<FRESH_DASHBOARD_FQDN>')
$ProbeRules = foreach ($ProbeName in $ProbeNames) {
    Add-DnsClientNrptRule -Namespace $ProbeName -NameServers '<HELPER_LAN_IPV4>' `
        -Comment 'Runway #603 isolated helper' -PassThru
}
$ProbeRules | Select-Object Name, Namespace, NameServers
```

This directs actual DNS queries to the fixture; it does not hardcode answers.
Do not use hosts-file entries, browser host-resolver flags, proxy mappings or
DevTools routing as proof of rebinding. `Resolve-DnsName -Server`/`dig` may verify
the fixture separately, but their logs are **resolver diagnostics**, not proof
that a browser re-resolved. Actual browser runs require correlated browser/helper
DNS/connection evidence. Preserve browser secure-DNS/proxy/protection settings;
if they bypass the isolated resolver, record that constraint rather than disabling
them. Check rule precedence and actual fixture queries before proceeding.
Missing or bypassed resolver rules can cause lookup failures or connections to
another host. Check actual DNS answers and the browser remote address; a helper
403 specifically indicates its Host/Origin guard refused that request, not proof
that resolver routing worked. Controller resolution never proves browser routing.
Microsoft documents [NRPT creation](https://learn.microsoft.com/powershell/module/dnsclient/add-dnsclientnrptrule)
and [removal](https://learn.microsoft.com/powershell/module/dnsclient/remove-dnsclientnrptrule).

Use disposable Edge/Chrome and Firefox profiles containing synthetic identities
only. The human handles every browser security prompt. Record permission denied,
then granted where supported, using the browser's normal site controls and a
fresh rebinding hostname per run. Never automate prompt decisions or change
security flags. Select the observed LNA state on the page before each attempt;
the default `not-recorded` is not acceptance evidence.

Chrome's documented rollout initially gates **public-network origins** accessing
private/loopback destinations. A private-LAN TLS helper may therefore show no LNA
prompt in the installed browser. Record the actual version and behavior. If that
browser requires a public origin, use an independently controlled public TLS
helper or mark denied/granted LNA rows blocked; do not manufacture a public origin
using address-space overrides. See [Chrome's LNA documentation](https://developer.chrome.com/blog/local-network-access).

## Requests and evidence

The page sends only synthetic mutation payloads and includes browser cookies from
the disposable profile; no dashboard owner key is embedded or supplied by it.

| Page | Direct or rebound requests |
| --- | --- |
| Sidecar | form POST `/save` with a synthetic replacement URL/key; GET `/` |
| Dashboard | JSON PUT `/api/v1/system/dashboard-layout` with synthetic provider ordering; GET `/`; GET `/api/v1/system/dashboard-layout` |

TLS pages disable the rebinding button because these Windows targets speak HTTP.
Use HTTP pages on matching ports for real same-origin rebinding. The arm POST
requires the page's Host/Origin and switches only that hostname to `127.0.0.1`.
Responses close connections; ten retry rounds run two seconds apart, with a
five-second per-fetch timeout. Browser DNS caching/pinning may still prevent a
fresh lookup; this is an observed browser constraint, not application acceptance.

Before testing, the Windows agent captures legitimate owner dashboard layout and
sidecar server URL/fixture-key configuration through ordinary authenticated owner
interfaces. Check the synthetic marker exists in isolated owner config; ordinary
settings HTML must omit it. Use **Check synthetic marker detector** separately:
positive must be true and negative false. The helper HTML itself omits the
contiguous marker, so a pinned helper read cannot impersonate key disclosure.

For each attempted refusal, compare legitimate owner state with its baseline,
confirm the installed process remains runnable, and verify ordinary same-origin
use works. Collect timestamped application logs from the Windows agent's isolated
fixtures. If they suppress normal access logs, instrument only that fixture's
ASGI response/send or settings-handler response hook to record method/path,
socket peer, Host, Origin and status; leave the real rejection logic active.
Do not log bodies, cookies, signatures, owner keys or settings values.

- Helper evidence: `helper-events.jsonl` contains UTC startup/source hashes,
  DNS transport/name/type/answer/TTL, arm events and helper-response statuses.
  Unconfigured query names are omitted. Never expose private certificate keys.
- Browser evidence: **Save sanitized results** downloads JSON containing only
  page origin, user agent, secure-context flag, selected permission state and
  timestamped method/path/status/error/marker booleans. Response bodies are read
  transiently for marker detection and are not exported. Console entries use the
  same safe records. Capture network outcomes/remote address without HAR bodies
  or credential headers.
- `helperResponse=true` means the browser is still contacting the helper; it is
  not an application rejection. Record `browser-blocked` if LNA/pinning or
  secure-context/mixed-content protections prevent the request reaching Runway.
  A second DNS answer alone is insufficient: correlate the real browser query,
  connection and Windows Host/Origin rejection. Distinguish preflight rejection
  from the mutation reaching the application. CORS may hide a real rejection
  from JavaScript even when the Windows application logs prove it occurred.

## Teardown and handoff status

Create the evidence directory under a protected parent with mode `0700` before
starting the helper; it creates a missing final directory with mode `0700` and
writes its log with mode `0600`. Existing directory permissions remain the
operator's responsibility, including intermediate parents created by `mkdir`.

The human removes only the recorded NRPT rules and verifies the prior effective
resolver configuration is restored:

```powershell
$ProbeRules | ForEach-Object { Remove-DnsClientNrptRule -Name $_.Name -Force }
```

Close/delete disposable profiles after exporting sanitized records. Have the
Windows agent remove its own temporary targets after acceptance. Stop the helper,
verify its sockets are absent, revoke its separate certificate/deactivate the
temporary ACME account, delete private keys and retain only sanitized evidence.
Any temporary DNS-01 challenge record must be removed by its exact created ID;
normal DNS records and existing TLS certificates stay intact.

A helper readiness check, verified Linux TLS chain, or diagnostic DNS switch does
not certify Windows browser behavior. Record unavailable inputs as blocked. Keep
the Windows agent's immutable signed ZIP/installer captures and exact Sigstore
metadata; rolling edge URLs must not replace those native evidence files.
