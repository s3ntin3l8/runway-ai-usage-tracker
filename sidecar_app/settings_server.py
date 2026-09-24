"""Tiny local HTTP settings server for the Runway Sidecar tray app.

Binds to 127.0.0.1 on port 17653 (tries up to 17672 if busy).
Serves a self-contained dark-themed settings form; saves changes to
config.json and hot-reloads the daemon without a restart.
"""

import hmac
import json
import logging
import os
import pathlib
import secrets
import socketserver
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from string import Template
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 17653


# ---------------------------------------------------------------------------
# HTML — self-contained, no external CDN, matches Runway dark aesthetic
# ---------------------------------------------------------------------------

_CSS = r"""*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  background: #09090b;
  color: #e4e4e7;
  min-height: 100vh;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 2.5rem 1rem 4rem;
}

.page { width: 100%; max-width: 600px; display: flex; flex-direction: column; gap: 1.25rem; }

.header { display: flex; align-items: center; gap: 0.75rem; padding: 0 0.25rem; }
.header-hex { font-size: 1.5rem; color: #a78bfa; }
.header-title { font-size: 1.2rem; font-weight: 700; color: #f4f4f5; letter-spacing: -0.02em; }
.header-sub { font-size: 0.7rem; color: #71717a; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 2px; }

.card {
  background: rgba(24,24,27,0.8);
  border: 1px solid rgba(63,63,70,0.6);
  border-radius: 1rem;
  padding: 1.25rem;
  backdrop-filter: blur(8px);
}

.section-label {
  font-size: 0.65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: #71717a;
  margin-bottom: 0.875rem;
}

.status-row { display: flex; align-items: center; gap: 0.625rem; }
.dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  box-shadow: 0 0 6px currentColor;
}
.dot-ok    { background: #22c55e; color: #22c55e; }
.dot-warn  { background: #f59e0b; color: #f59e0b; }
.dot-err   { background: #ef4444; color: #ef4444; }
.dot-paused { background: #71717a; color: #71717a; }
.dot-starting { background: #f59e0b; color: #f59e0b; }

.status-text { font-size: 0.8rem; color: #a1a1aa; }
.status-text strong { color: #e4e4e7; font-weight: 600; }
.status-detail { font-size: 0.7rem; color: #52525b; margin-top: 0.3rem; font-family: "SF Mono", "Cascadia Code", "Consolas", monospace; }

.field { display: flex; flex-direction: column; gap: 0.35rem; }
.field + .field { margin-top: 0.875rem; }

label { font-size: 0.75rem; font-weight: 600; color: #a1a1aa; }

.input-wrap { position: relative; }
input[type="text"],
input[type="password"],
input[type="url"],
input[type="number"],
select {
  width: 100%;
  background: rgba(9,9,11,0.8);
  border: 1px solid rgba(63,63,70,0.8);
  border-radius: 0.5rem;
  color: #e4e4e7;
  font-size: 0.85rem;
  font-family: inherit;
  padding: 0.55rem 0.75rem;
  outline: none;
  transition: border-color 0.15s;
  -webkit-appearance: none;
  appearance: none;
}
input:focus, select:focus {
  border-color: #7c3aed;
  box-shadow: 0 0 0 2px rgba(124,58,237,0.2);
}
input.has-toggle { padding-right: 2.5rem; }

.toggle-vis {
  position: absolute;
  right: 0.6rem;
  top: 50%;
  transform: translateY(-50%);
  background: none;
  border: none;
  cursor: pointer;
  color: #52525b;
  padding: 2px;
  line-height: 0;
  transition: color 0.15s;
}
.toggle-vis:hover { color: #a78bfa; }

select {
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2371717a' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 0.7rem center;
  padding-right: 2.25rem;
  cursor: pointer;
}

.field-hint { font-size: 0.68rem; color: #52525b; margin-top: 0.15rem; }

.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem;
  font-size: 0.85rem; font-weight: 600; font-family: inherit;
  padding: 0.6rem 1.25rem;
  border-radius: 0.5rem;
  border: none;
  cursor: pointer;
  transition: all 0.15s;
  outline: none;
  text-decoration: none;
}
.btn-primary {
  background: #7c3aed;
  color: #fff;
}
.btn-primary:hover { background: #6d28d9; }
.btn-primary:active { background: #5b21b6; transform: scale(0.98); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

.btn-ghost {
  background: transparent;
  color: #71717a;
  border: 1px solid rgba(63,63,70,0.6);
}
.btn-ghost:hover { color: #e4e4e7; border-color: rgba(113,113,122,0.6); }

.actions { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
.links { display: flex; gap: 1rem; }
.link { font-size: 0.75rem; color: #52525b; text-decoration: none; cursor: pointer; transition: color 0.15s; }
.link:hover { color: #a78bfa; }

.toast {
  font-size: 0.8rem;
  padding: 0.5rem 0.875rem;
  border-radius: 0.5rem;
  display: none;
  align-items: center;
  gap: 0.4rem;
}
.toast.show { display: flex; }
.toast-ok  { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.25); color: #86efac; }
.toast-err { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.25); color: #fca5a5; }

.footer { text-align: center; font-size: 0.68rem; color: #3f3f46; padding-top: 0.5rem; }

.log-pre {
  font-size: 0.63rem; line-height: 1.55; color: #52525b;
  background: rgba(9,9,11,0.6); border: 1px solid rgba(63,63,70,0.4);
  border-radius: 0.5rem; padding: 0.75rem;
  max-height: 260px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-all;
  font-family: "SF Mono", "Cascadia Code", "Consolas", monospace;
}
"""

_HTML = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Runway Sidecar — Settings</title>
<style>
$css</style>
</head>
<body>
<div class="page">

  <!-- Header -->
  <div class="header">
    <span class="header-hex">⬡</span>
    <div>
      <div class="header-title">Runway Sidecar</div>
      <div class="header-sub">Settings</div>
    </div>
  </div>

  <!-- Status card -->
  <div class="card" id="status-card">
    <div class="section-label">Status</div>
    <div class="status-row">
      <div class="dot dot-starting" id="status-dot"></div>
      <div class="status-text" id="status-text"><strong>Loading…</strong></div>
    </div>
    <div class="status-detail" id="status-detail"></div>
  </div>

  <!-- Settings form -->
  <form class="card" id="settings-form" onsubmit="saveSettings(event)">
    <div class="section-label">Server</div>

    <div class="field">
      <label for="api_url">API URL</label>
      <input type="url" id="api_url" name="api_url" value="$api_url"
             placeholder="http://localhost:8765" required>
      <span class="field-hint">The address of your Runway server. Have a pairing code from the
        dashboard's Fleet page? <a class="link" href="/pair">Pair with a code…</a></span>
    </div>

    <div class="field">
      <label for="api_key">API Key</label>
      <div class="input-wrap">
        <input type="password" id="api_key" name="api_key" value="$api_key"
               class="has-toggle" placeholder="Your INGEST_API_KEY" autocomplete="current-password">
        <button type="button" class="toggle-vis" onclick="toggleKey()" title="Show / hide key" id="toggle-btn">
          <svg id="eye-icon" xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24"
               fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
          </svg>
        </button>
      </div>
    </div>

    <div style="margin-top:1.5rem" class="actions">
      <button type="submit" class="btn btn-primary" id="save-btn">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24"
             fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
        Save &amp; Apply
      </button>
      <div class="toast toast-ok" id="toast-ok">✓ Saved and applied</div>
      <div class="toast toast-err" id="toast-err">⚠ <span id="toast-err-msg">Error</span></div>
    </div>
  </form>

  <!-- Quick links -->
  <div class="card" style="padding: 1rem 1.25rem;">
    <div class="section-label">Quick Links</div>
    <div class="links">
      <a class="link" onclick="openDashboard()">↗ Open Dashboard</a>
    </div>
  </div>

  <!-- Log viewer -->
  <div class="card" id="log-card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem">
      <div class="section-label" style="margin:0">Recent Logs</div>
      <div style="display:flex;gap:0.5rem;align-items:center">
        <label style="display:flex;align-items:center;gap:0.35rem;font-size:0.7rem;color:#52525b;cursor:pointer">
          <input type="checkbox" id="log-auto-refresh" checked style="width:auto;accent-color:#7c3aed">
          Auto
        </label>
        <button type="button" class="btn btn-ghost" style="padding:0.25rem 0.6rem;font-size:0.7rem" onclick="refreshLogs()">↻ Refresh</button>
      </div>
    </div>
    <pre class="log-pre" id="log-output">Loading…</pre>
  </div>

  <div class="footer" id="footer-version">Runway Sidecar v$version &nbsp;·&nbsp; $sidecar_id</div>

</div>

<script>
// ---- Status polling -------------------------------------------------------

const STATUS_LABELS = {
  ok: 'Healthy', warn: 'Warning', err: 'Error',
  paused: 'Paused', starting: 'Starting…'
};
const DOT_CLASS = {
  ok: 'dot-ok', warn: 'dot-warn', err: 'dot-err',
  paused: 'dot-paused', starting: 'dot-starting'
};

function updateStatus(s) {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  const det = document.getElementById('status-detail');
  dot.className = 'dot ' + (DOT_CLASS[s.status] || 'dot-starting');
  txt.innerHTML = '<strong>' + (STATUS_LABELS[s.status] || s.status) + '</strong>';
  if (s.stats) det.textContent = s.stats;
}

async function pollStatus() {
  try {
    const r = await fetch('/status');
    if (r.ok) updateStatus(await r.json());
  } catch {}
}

pollStatus();
setInterval(pollStatus, 5000);

// ---- Key visibility toggle ------------------------------------------------

function toggleKey() {
  const inp = document.getElementById('api_key');
  const icon = document.getElementById('eye-icon');
  if (inp.type === 'password') {
    inp.type = 'text';
    icon.innerHTML = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';
  } else {
    inp.type = 'password';
    icon.innerHTML = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
  }
}

// ---- Save -----------------------------------------------------------------

async function saveSettings(e) {
  e.preventDefault();
  const btn = document.getElementById('save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  hideToasts();

  const data = new URLSearchParams({
    api_url: document.getElementById('api_url').value,
    api_key: document.getElementById('api_key').value,
  });

  try {
    const r = await fetch('/save', { method: 'POST', body: data,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
    const j = await r.json();
    if (j.ok) {
      showToast('ok');
      pollStatus();
    } else {
      showToast('err', j.error || 'Unknown error');
    }
  } catch (err) {
    showToast('err', err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Save &amp; Apply';
  }
}

function showToast(type, msg) {
  hideToasts();
  if (type === 'ok') {
    document.getElementById('toast-ok').classList.add('show');
    setTimeout(() => document.getElementById('toast-ok').classList.remove('show'), 3000);
  } else {
    document.getElementById('toast-err-msg').textContent = msg || 'Error';
    document.getElementById('toast-err').classList.add('show');
    setTimeout(() => document.getElementById('toast-err').classList.remove('show'), 5000);
  }
}

function hideToasts() {
  document.getElementById('toast-ok').classList.remove('show');
  document.getElementById('toast-err').classList.remove('show');
}

// ---- Quick links -------------------------------------------------------

function openDashboard() { fetch('/action/dashboard', {method:'POST'}).catch(()=>{}); }

// ---- Log viewer --------------------------------------------------------

async function refreshLogs() {
  try {
    const r = await fetch('/logs');
    if (!r.ok) return;
    const j = await r.json();
    const pre = document.getElementById('log-output');
    pre.textContent = (j.lines && j.lines.length) ? j.lines.join('\n') : '(no log entries yet)';
    pre.scrollTop = pre.scrollHeight;
  } catch {}
}

refreshLogs();
setInterval(() => {
  if (document.getElementById('log-auto-refresh').checked) refreshLogs();
}, 5000);
</script>
</body>
</html>
""")


# Confirmation page for runway-sidecar://pair links (and manual code entry).
# Pairing re-points where this machine sends usage data and provider
# credentials, so it never happens without this explicit, server-naming click.
_PAIR_HTML = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Runway Sidecar — Pair</title>
<style>$css
.target { font-size: 1.05rem; font-weight: 700; color: #f4f4f5; word-break: break-all; margin: 0.25rem 0 0.75rem; }
.warn { font-size: 0.75rem; line-height: 1.5; color: #fcd34d; background: rgba(245,158,11,0.08);
        border: 1px solid rgba(245,158,11,0.25); border-radius: 0.5rem; padding: 0.6rem 0.75rem; margin-top: 0.75rem; }
.muted { font-size: 0.75rem; color: #a1a1aa; line-height: 1.5; }
input[readonly] { color: #a1a1aa; }
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <span class="header-hex">⬡</span>
    <div>
      <div class="header-title">Runway Sidecar</div>
      <div class="header-sub">Pair with a server</div>
    </div>
  </div>

  <form class="card" id="pair-form" onsubmit="pair(event)">
    <div class="section-label">Connect this machine to</div>
    <div class="target" id="target">$server_display</div>
    <div class="field">
      <label for="server">Server address</label>
      <input type="url" id="server" name="server" value="$server" $readonly
             placeholder="https://runway.example.com" required>
    </div>
    <div class="field">
      <label for="code">Pairing code</label>
      <input type="text" id="code" name="code" value="$code" $readonly
             placeholder="XXXXX-XXXXX" autocomplete="off" spellcheck="false" required>
      <span class="field-hint">From <em>Fleet → Add sidecar → Pair</em> in the Runway dashboard. Codes work once and expire after a few minutes.</span>
    </div>
    $replace_note
    <div class="warn">Only continue if you just asked for this pairing in <strong>your own</strong>
      Runway dashboard. Once paired, this sidecar sends AI usage data and provider sign-in
      tokens from this machine to the server above.</div>
    <div style="margin-top:1.25rem" class="actions">
      <button type="submit" class="btn btn-primary" id="pair-btn">Pair</button>
      <a class="btn btn-ghost" href="/">Cancel</a>
      <div class="toast toast-ok" id="toast-ok">✓ <span id="toast-ok-msg">Paired</span></div>
      <div class="toast toast-err" id="toast-err">⚠ <span id="toast-err-msg">Error</span></div>
    </div>
  </form>
  <div class="footer">Runway Sidecar v$version</div>
</div>
<script>
const serverInput = document.getElementById('server');
serverInput.addEventListener('input', () => {
  document.getElementById('target').textContent = serverInput.value || '—';
});
async function pair(ev) {
  ev.preventDefault();
  const btn = document.getElementById('pair-btn');
  const ok = document.getElementById('toast-ok');
  const err = document.getElementById('toast-err');
  ok.classList.remove('show'); err.classList.remove('show');
  btn.disabled = true; btn.textContent = 'Pairing…';
  try {
    const r = await fetch('/pair', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams(new FormData(document.getElementById('pair-form'))),
    });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || ('HTTP ' + r.status));
    document.getElementById('toast-ok-msg').textContent =
      'Paired with ' + j.api_url + '. Collection starts now; you can close this tab.';
    ok.classList.add('show');
    btn.textContent = 'Paired';
  } catch (e) {
    document.getElementById('toast-err-msg').textContent = e.message;
    err.classList.add('show');
    btn.disabled = false; btn.textContent = 'Pair';
  }
}
</script>
</body>
</html>
""")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: "_SettingsServer"  # type annotation for IDE

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        pass  # suppress access log noise

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._serve_settings_page()
        elif path == "/pair":
            self._serve_pair_page()
        elif path == "/status":
            self._serve_status()
        elif path == "/logs":
            self._serve_logs()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        # Hand-off from a second sidecar process that was launched with a
        # runway-sidecar:// URL (Windows protocol handler). Authenticated by the
        # per-run token in the owner-only control file, not by Origin — it is
        # not a browser request. A browser can't forge it: the custom header
        # forces a CORS preflight we never answer, and it can't read the token.
        if urlparse(self.path).path == "/pair-request":
            self._handle_pair_request()
            return
        # CSRF guard: modern browsers send Origin on EVERY POST (same- or
        # cross-origin) since ~2020. We require it to be present AND match
        # our bound address — falling back to "missing = trusted" was the
        # historical loophole some non-browser clients exploited.
        origin = self.headers.get("Origin", "")
        allowed = f"http://127.0.0.1:{self.server.server_address[1]}"
        if not origin or not origin.startswith(allowed):
            self.send_error(403, "Forbidden")
            return

        path = urlparse(self.path).path
        if path == "/save":
            self._handle_save()
        elif path == "/pair":
            self._handle_pair()
        elif path.startswith("/action/"):
            self._handle_action(path[len("/action/") :])
        else:
            self.send_error(404)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _serve_settings_page(self) -> None:
        config = self.server.get_config()
        status = self.server.get_status()
        html = _HTML.substitute(
            css=_CSS,
            api_url=_esc(config.get("api_url", "")),
            api_key=_esc(config.get("api_key", "")),
            version=_esc(status.get("version", "?")),
            sidecar_id=_esc(status.get("sidecar_id", "")),
        )
        self._send_html(html)

    def _serve_status(self) -> None:
        self._send_json(self.server.get_status())

    def _serve_logs(self) -> None:
        from sidecar_app.config import get_log_path

        try:
            log_path = get_log_path()
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-200:]
            self._send_json({"lines": [line.rstrip() for line in lines]})
        except Exception as exc:
            self._send_json({"lines": [], "error": str(exc)})

    def _handle_save(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        params = parse_qs(raw, keep_blank_values=True)

        def first(key: str, default: str = "") -> str:
            vals = params.get(key, [default])
            return vals[0].strip() if vals else default

        api_url = first("api_url")
        api_key = first("api_key")

        if not api_url:
            self._send_json({"ok": False, "error": "API URL is required"}, 400)
            return

        new_config = dict(self.server.get_config())
        new_config["api_url"] = api_url
        new_config["api_key"] = api_key

        try:
            self.server.save_config(new_config)
            self._send_json({"ok": True})
        except Exception as exc:
            logger.error(f"Settings save error: {exc}")
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _serve_pair_page(self) -> None:
        from scripts.sidecar_pkg import pairing

        query = parse_qs(urlparse(self.path).query)
        server = (query.get("server") or [""])[0]
        code = (query.get("code") or [""])[0]
        from_link = bool(server and code)
        try:
            shown = pairing.normalize_server(server) if server else ""
        except pairing.PairingError:
            shown = server
        config = self.server.get_config()
        current = str(config.get("api_url") or "")
        has_key = bool(config.get("api_key")) and config.get("api_key") != "REPLACE_ME"
        replace_note = ""
        if has_key and current and shown and current.rstrip("/") != shown:
            replace_note = (
                '<div class="warn">This replaces the server this sidecar currently reports to: '
                f"<strong>{_esc(current)}</strong></div>"
            )
        status = self.server.get_status()
        self._send_html(
            _PAIR_HTML.substitute(
                css=_CSS,
                server=_esc(server),
                code=_esc(code),
                server_display=_esc(shown or "—"),
                readonly="readonly" if from_link else "",
                replace_note=replace_note,
                version=_esc(status.get("version", "?")),
            )
        )

    def _handle_pair(self) -> None:
        from scripts.sidecar_pkg import pairing

        length = int(self.headers.get("Content-Length", 0))
        params = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        try:
            target = pairing.PairTarget(
                server=pairing.normalize_server((params.get("server") or [""])[0]),
                code=pairing.normalize_code((params.get("code") or [""])[0]),
            )
            hostname = self.server.get_status().get("sidecar_id") or None
            creds = pairing.redeem(target, hostname=hostname)
        except pairing.PairingError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        new_config = dict(self.server.get_config())
        new_config.update(creds)
        try:
            self.server.save_config(new_config)
        except Exception as exc:
            logger.error(f"Pairing save error: {exc}")
            self._send_json({"ok": False, "error": f"Paired, but saving failed: {exc}"}, 500)
            return
        logger.info(f"Paired with {creds['api_url']}")
        self._send_json({"ok": True, "api_url": creds["api_url"]})

    def _handle_pair_request(self) -> None:
        token = self.headers.get("X-Runway-Control", "")
        if not self.server.control_token or not hmac.compare_digest(
            token, self.server.control_token
        ):
            self.send_error(403, "Forbidden")
            return
        length = min(int(self.headers.get("Content-Length", 0)), 8192)
        try:
            url = str(json.loads(self.rfile.read(length) or b"{}").get("url", ""))
        except ValueError:
            url = ""
        if self.server.on_pair_url is None or not url:
            self.send_error(400)
            return
        self.server.on_pair_url(url)
        self._send_json({"ok": True})

    def _handle_action(self, action: str) -> None:
        try:
            if action == "dashboard":
                self.server.open_dashboard()
            elif action == "logs":
                self.server.open_logs()
            elif action == "config":
                self.server.open_config()
            self._send_json({"ok": True})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _esc(s: str) -> str:
    """HTML-escape a string for safe embedding in attribute values."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Custom TCPServer that carries shared state
# ---------------------------------------------------------------------------


class _SettingsServer(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(
        self,
        host: str,
        port: int,
        get_config: Callable[[], dict],
        get_status: Callable[[], dict],
        save_config: Callable[[dict], None],
        open_dashboard: Callable[[], None],
        open_logs: Callable[[], None],
        open_config: Callable[[], None],
    ) -> None:
        super().__init__((host, port), _Handler)
        self.control_token = ""
        self.on_pair_url: Callable[[str], None] | None = None
        self.get_config = get_config
        self.get_status = get_status
        self.save_config = save_config
        self.open_dashboard = open_dashboard
        self.open_logs = open_logs
        self.open_config = open_config


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SettingsServer:
    """Manages the lifecycle of the local settings HTTP server."""

    def __init__(
        self,
        get_config: Callable[[], dict],
        get_status: Callable[[], dict],
        save_config: Callable[[dict], None],
        open_dashboard: Callable[[], None],
        open_logs: Callable[[], None],
        open_config: Callable[[], None],
        port: int = _DEFAULT_PORT,
    ) -> None:
        self._get_config = get_config
        self._get_status = get_status
        self._save_config = save_config
        self._open_dashboard = open_dashboard
        self._open_logs = open_logs
        self._open_config = open_config
        self._port = port
        self._server: _SettingsServer | None = None
        self._thread: threading.Thread | None = None
        # Authenticates /pair-request hand-offs from a second process; shared
        # with it through the owner-only control file (see write_control_file).
        self.control_token = secrets.token_urlsafe(32)
        # Called with a user-facing message when a pairing link is unusable.
        self.notify: Callable[[str], None] = lambda msg: logger.warning(msg)

    def start(self) -> int:
        """Start the server. Returns the actual port it bound to."""
        port = self._port
        for _ in range(20):
            try:
                self._server = _SettingsServer(
                    "127.0.0.1",
                    port,
                    self._get_config,
                    self._get_status,
                    self._save_config,
                    self._open_dashboard,
                    self._open_logs,
                    self._open_config,
                )
                break
            except OSError:
                port += 1
        else:
            raise RuntimeError("Settings server: could not bind to any port in range")

        self._port = self._server.server_address[1]  # the bound port (port=0 → OS-assigned)
        self._server.control_token = self.control_token
        self._server.on_pair_url = self.open_pair
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="SettingsServer",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Settings server at http://127.0.0.1:{self._port}")
        return self._port

    @property
    def port(self) -> int:
        return self._port

    def open(self) -> None:
        """Open the settings page in the default browser."""
        webbrowser.open(f"http://127.0.0.1:{self._port}/")

    def open_pair(self, url: str | None = None) -> None:
        """Show the pairing confirmation page for a runway-sidecar:// *url*.

        Never pairs by itself: it only opens the page on which the user
        confirms (or cancels) after seeing the target server. With no *url*
        it opens the manual "pair with a code" form.
        """
        from scripts.sidecar_pkg import pairing

        query = ""
        if url:
            try:
                target = pairing.parse_pair_url(url)
            except pairing.PairingError as exc:
                self.notify(f"Couldn't use that pairing link: {exc}")
                return
            query = "?" + urlencode({"server": target.server, "code": target.code})
        webbrowser.open(f"http://127.0.0.1:{self._port}/pair{query}")

    def write_control_file(self, path: pathlib.Path) -> None:
        """Publish ``{port, token, pid}`` so a second process can hand off a link.

        Owner-only (0600), written atomically; removed by ``stop()``.
        """
        payload = json.dumps({"port": self._port, "token": self.control_token, "pid": os.getpid()})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
        self._control_file = path

    def stop(self) -> None:
        """Shut down the server gracefully."""
        control = getattr(self, "_control_file", None)
        if control is not None:
            try:
                control.unlink()
            except OSError:
                # Already removed; a stale file is harmless (its token dies
                # with this process and the port is re-probed on connect).
                pass
        if self._server:
            self._server.shutdown()
            self._server = None
