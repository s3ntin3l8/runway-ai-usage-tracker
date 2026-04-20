"""Tiny local HTTP settings server for the Runway Sidecar tray app.

Binds to 127.0.0.1 on port 17653 (tries up to 17672 if busy).
Serves a self-contained dark-themed settings form; saves changes to
config.json and hot-reloads the daemon without a restart.
"""

import json
import logging
import socketserver
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from string import Template
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 17653

# Provider list — mirrors __REGISTRY__ keys in scripts/sidecar.py
_PROVIDER_LIST: list[tuple[str, str]] = [
    ("anthropic", "Claude Pro"),
    ("github", "GitHub Copilot"),
    ("gemini", "Gemini API"),
    ("chatgpt", "ChatGPT / Codex"),
    ("opencode", "OpenCode"),
    ("antigravity", "Antigravity"),
    ("ollama", "Ollama Cloud"),
    ("openrouter", "OpenRouter"),
    ("minimax", "MiniMax"),
    ("kimi", "Kimi API"),
    ("zai", "zAI API"),
]


def _make_providers_html(enabled: list) -> str:
    """Return checkbox HTML for the providers section."""
    all_enabled = not enabled or "all" in enabled
    parts = []
    for pid, name in _PROVIDER_LIST:
        checked = "checked" if (all_enabled or pid in enabled) else ""
        parts.append(
            f'<label class="provider-check">'
            f'<input type="checkbox" name="providers" value="{pid}" {checked}>'
            f"<span>{_esc(name)}</span></label>"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML — self-contained, no external CDN, matches Runway dark aesthetic
# ---------------------------------------------------------------------------

_HTML = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Runway Sidecar — Settings</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

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

.provider-check {
  display: flex; align-items: center; gap: 0.5rem;
  cursor: pointer; font-size: 0.78rem; color: #a1a1aa;
  padding: 0.2rem 0; user-select: none;
}
.provider-check input[type="checkbox"] { width: auto; accent-color: #7c3aed; cursor: pointer; }
.provider-check:hover { color: #e4e4e7; }

.log-pre {
  font-size: 0.63rem; line-height: 1.55; color: #52525b;
  background: rgba(9,9,11,0.6); border: 1px solid rgba(63,63,70,0.4);
  border-radius: 0.5rem; padding: 0.75rem;
  max-height: 260px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-all;
  font-family: "SF Mono", "Cascadia Code", "Consolas", monospace;
}
</style>
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
      <span class="field-hint">The address of your Runway server.</span>
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

    <div class="section-label" style="margin-top:1.25rem">Collection</div>

    <div class="field">
      <label for="interval_select">Sync interval</label>
      <select id="interval_select" onchange="onIntervalSelect(this)">
        <option value="300"  $sel300>5 minutes</option>
        <option value="900"  $sel900>15 minutes</option>
        <option value="1800" $sel1800>30 minutes</option>
        <option value="3600" $sel3600>1 hour</option>
        <option value="7200" $sel7200>2 hours</option>
        <option value="custom" $selcustom>Custom…</option>
      </select>
    </div>

    <div class="field" id="custom-interval-field" style="display:$custom_display">
      <label for="interval_seconds">Custom interval (seconds)</label>
      <input type="number" id="interval_seconds" name="interval_seconds"
             value="$interval_seconds" min="60" max="86400" step="60">
      <span class="field-hint">Minimum 60 seconds.</span>
    </div>
    <input type="hidden" id="interval_hidden" name="interval_seconds" value="$interval_seconds">

    <div class="section-label" style="margin-top:1.25rem">Providers</div>
    <div class="field-hint" style="margin-bottom:0.75rem">Uncheck providers you do not use to skip collection.</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.25rem 1.5rem">
      $providers_html
    </div>
    <input type="hidden" name="providers_submitted" value="1">

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

// ---- Interval select ------------------------------------------------------

const PRESETS = new Set(['300','900','1800','3600','7200']);

function onIntervalSelect(sel) {
  const customField = document.getElementById('custom-interval-field');
  const hidden = document.getElementById('interval_hidden');
  if (sel.value === 'custom') {
    customField.style.display = '';
  } else {
    customField.style.display = 'none';
    hidden.value = sel.value;
  }
}

document.getElementById('interval_select').addEventListener('change', function() {
  if (this.value !== 'custom') {
    document.getElementById('interval_hidden').value = this.value;
  }
});

document.getElementById('interval_seconds').addEventListener('input', function() {
  document.getElementById('interval_hidden').value = this.value;
});

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
    api_url:          document.getElementById('api_url').value,
    api_key:          document.getElementById('api_key').value,
    interval_seconds: document.getElementById('interval_hidden').value,
    providers_submitted: '1',
  });
  document.querySelectorAll('input[name="providers"]').forEach(cb => {
    if (cb.checked) data.append('providers', cb.value);
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
        elif path == "/status":
            self._serve_status()
        elif path == "/logs":
            self._serve_logs()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        # CSRF guard: browsers always send Origin on cross-origin POSTs.
        # Reject any request whose Origin header doesn't match our own address.
        origin = self.headers.get("Origin", "")
        allowed = f"http://127.0.0.1:{self.server.server_address[1]}"
        if origin and not origin.startswith(allowed):
            self.send_error(403, "Forbidden")
            return

        path = urlparse(self.path).path
        if path == "/save":
            self._handle_save()
        elif path.startswith("/action/"):
            self._handle_action(path[len("/action/") :])
        else:
            self.send_error(404)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _serve_settings_page(self) -> None:
        config = self.server.get_config()
        interval = int(config.get("interval_seconds", 1800))
        presets = {300, 900, 1800, 3600, 7200}
        is_custom = interval not in presets

        def sel(v: int) -> str:
            return "selected" if interval == v and not is_custom else ""

        status = self.server.get_status()
        enabled_providers = config.get("providers", ["all"])
        html = _HTML.substitute(
            api_url=_esc(config.get("api_url", "")),
            api_key=_esc(config.get("api_key", "")),
            interval_seconds=interval,
            sel300=sel(300),
            sel900=sel(900),
            sel1800=sel(1800),
            sel3600=sel(3600),
            sel7200=sel(7200),
            selcustom="selected" if is_custom else "",
            custom_display="" if is_custom else "none",
            version=_esc(status.get("version", "?")),
            sidecar_id=_esc(status.get("sidecar_id", "")),
            providers_html=_make_providers_html(enabled_providers),
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
        try:
            interval = max(60, int(first("interval_seconds", "900")))
        except ValueError:
            interval = 900

        if not api_url:
            self._send_json({"ok": False, "error": "API URL is required"}, 400)
            return

        new_config = dict(self.server.get_config())
        new_config["api_url"] = api_url
        new_config["api_key"] = api_key
        new_config["interval_seconds"] = interval

        # Persist provider list when the form includes the providers_submitted sentinel
        if first("providers_submitted") == "1":
            checked = params.get("providers", [])
            all_ids = {pid for pid, _ in _PROVIDER_LIST}
            new_config["providers"] = ["all"] if set(checked) >= all_ids else list(checked)

        try:
            self.server.save_config(new_config)
            self._send_json({"ok": True})
        except Exception as exc:
            logger.error(f"Settings save error: {exc}")
            self._send_json({"ok": False, "error": str(exc)}, 500)

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

        self._port = port
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="SettingsServer",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Settings server at http://127.0.0.1:{port}")
        return port

    def open(self) -> None:
        """Open the settings page in the default browser."""
        webbrowser.open(f"http://127.0.0.1:{self._port}/")

    def stop(self) -> None:
        """Shut down the server gracefully."""
        if self._server:
            self._server.shutdown()
            self._server = None
