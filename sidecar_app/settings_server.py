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
import socket
import socketserver
import threading
import webbrowser
from collections import deque
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
$css
/* ---- Keychain scrapers ---- */
.kc-summary { display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.75rem; color: #a1a1aa; margin-top: 0.9rem;
  padding: 0.6rem 0.75rem; background: rgba(9,9,11,0.6); border: 1px solid rgba(63,63,70,0.5); border-radius: 0.6rem; }
.kc-summary strong { color: #f4f4f5; }
.kc-warn { color: #fcd34d; } .kc-warn strong { color: #fcd34d; } .kc-bad { color: #fca5a5; } .kc-bad strong { color: #fca5a5; }
.kc-group { margin-top: 1.1rem; }
.kc-group-label { font-size: 0.62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: #52525b;
  margin-bottom: 0.35rem; display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.kc-rows { border: 1px solid rgba(63,63,70,0.45); border-radius: 0.6rem; overflow: hidden; }
.kc-row { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 0.7rem; padding: 0.5rem 0.75rem; font-size: 0.8rem; }
.kc-row + .kc-row, .kc-more { border-top: 1px solid rgba(63,63,70,0.35); }
.kc-name { display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap; min-width: 0; }
.kc-info { color: #3f3f46; cursor: help; font-size: 0.75rem; }
.kc-tag { font-size: 0.58rem; font-weight: 800; letter-spacing: 0.06em; color: #a78bfa; border: 1px solid rgba(167,139,250,0.5); border-radius: 4px; padding: 0 4px; }
.kc-switch { position: relative; width: 30px; height: 17px; display: inline-block; }
.kc-switch input { opacity: 0; width: 100%; height: 100%; position: absolute; inset: 0; margin: 0; cursor: pointer; z-index: 1; }
.kc-switch span { position: absolute; inset: 0; background: #3f3f46; border-radius: 999px; transition: .15s; }
.kc-switch span::before { content: ""; position: absolute; width: 13px; height: 13px; left: 2px; top: 2px; background: #a1a1aa; border-radius: 50%; transition: .15s; }
.kc-switch input:checked + span { background: #7c3aed; } .kc-switch input:checked + span::before { transform: translateX(13px); background: #fff; }
.kc-switch.mixed input + span { background: #5b21b6; } .kc-switch.mixed input + span::before { transform: translateX(6.5px); background: #ddd6fe; }
.kc-switch input:disabled { cursor: not-allowed; } .kc-switch input:disabled + span { opacity: .35; }
.kc-switch input:focus-visible + span { box-shadow: 0 0 0 2px rgba(124,58,237,0.5); }
.kc-chip { display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.68rem; font-weight: 600; padding: 0.12rem 0.55rem; border-radius: 999px; white-space: nowrap; border: 1px solid transparent; }
.kc-chip::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.kc-ok { color: #86efac; background: rgba(34,197,94,.1); border-color: rgba(34,197,94,.25); }
.kc-need { color: #fcd34d; background: rgba(245,158,11,.1); border-color: rgba(245,158,11,.25); }
.kc-err { color: #fca5a5; background: rgba(239,68,68,.1); border-color: rgba(239,68,68,.3); }
.kc-busy { color: #93c5fd; background: rgba(59,130,246,.12); border-color: rgba(59,130,246,.3); } .kc-busy::before { animation: kcpulse .9s infinite alternate; }
.kc-wait { color: #a1a1aa; background: rgba(63,63,70,.3); border-color: rgba(63,63,70,.6); }
.kc-off { color: #71717a; border-color: rgba(63,63,70,.6); } .kc-off::before { background: transparent; border: 1px solid currentColor; }
@keyframes kcpulse { from { opacity: .3; } to { opacity: 1; } }
.kc-pill { display: inline-flex; align-items: center; gap: 0.3rem; font-size: 0.64rem; color: #a1a1aa; border: 1px solid rgba(63,63,70,.6); border-radius: 999px; padding: 0.05rem 0.45rem; }
.kc-pill i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.kc-d-ok { background: #22c55e; } .kc-d-need { background: #f59e0b; } .kc-d-err { background: #ef4444; } .kc-d-busy { background: #3b82f6; } .kc-d-wait, .kc-d-off { border: 1px solid #71717a; }
.kc-bsel { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; text-transform: none; letter-spacing: 0; font-weight: 500; }
.kc-bsel label { display: inline-flex; align-items: center; gap: 0.3rem; font-size: 0.7rem; color: #a1a1aa; border: 1px solid rgba(63,63,70,.7); border-radius: 999px; padding: 0.12rem 0.55rem; cursor: pointer; font-weight: 500; }
.kc-bsel label.sel { color: #e4e4e7; border-color: #7c3aed; background: rgba(124,58,237,.12); }
.kc-bsel input { position: absolute; opacity: 0; width: 0; height: 0; }
.kc-bsel label:focus-within { box-shadow: 0 0 0 2px rgba(124,58,237,0.5); }
.kc-more { font-size: 0.72rem; color: #71717a; padding: 0.45rem 0.75rem; cursor: pointer; background: none; border-left: 0; border-right: 0; border-bottom: 0; width: 100%; text-align: left; font-family: inherit; }
.kc-more:hover { color: #e4e4e7; }
.kc-renewal { margin-top: 1rem; display: flex; gap: .55rem; align-items: flex-start; font-size: .75rem; color: #a1a1aa; }
.kc-renewal input { margin-top: 2px; accent-color: #7c3aed; }
.kc-batch { margin-top: 1.1rem; padding-top: 1rem; border-top: 1px solid rgba(63,63,70,.5); }
.kc-progress { height: 3px; background: #27272a; border-radius: 3px; margin-top: .75rem; overflow: hidden; } .kc-progress div { height: 100%; background: #7c3aed; transition: width .3s; }
.kc-banner { margin-top: .9rem; font-size: .75rem; line-height: 1.5; border-radius: .6rem; padding: .65rem .8rem; }
.kc-b-warn { color: #fcd34d; background: rgba(245,158,11,.08); border: 1px solid rgba(245,158,11,.25); }
.kc-b-err { color: #fca5a5; background: rgba(239,68,68,.08); border: 1px solid rgba(239,68,68,.3); }
.kc-b-new { color: #c4b5fd; background: rgba(124,58,237,.08); border: 1px solid rgba(124,58,237,.3); display: flex; justify-content: space-between; gap: .5rem; align-items: center; }
.kc-b-new button { background: none; border: none; color: #a78bfa; font: inherit; font-weight: 700; cursor: pointer; }
.kc-banner a { color: inherit; font-weight: 700; }
#sources-error { font-size: .72rem; color: #fca5a5; margin-top: .6rem; }
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
      <span class="field-hint">The address of your Runway server. Have a pairing code from the
        dashboard's Fleet page? <a class="link" href="/pair">Pair with a code…</a></span>
    </div>

    <div class="field">
      <label for="api_key">API Key</label>
      <div class="input-wrap">
        <input type="password" id="api_key" name="api_key" value=""
               class="has-toggle" placeholder="Leave blank to keep the existing key" autocomplete="current-password">
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

  <section class="card" id="sources-card" aria-labelledby="sources-heading" hidden>
    <h2 class="section-label" id="sources-heading">Keychain scrapers</h2>
    <p class="field-hint">Choose which scrapers may read this Mac’s Keychain. File and environment sources continue normally. Access credentials may be sent to your paired server; refresh tokens stay on this Mac.</p>
    <div id="sources-body"></div>
    <p id="sources-error" role="alert"></p>
  </section>

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

async function sourceAction(action, source_id, value, extra) {
  try {
    const response = await fetch('/sources', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(Object.assign({action, source_id, value}, extra || {}))});
    if (!response.ok) throw new Error('Could not apply source action');
    document.getElementById('sources-error').textContent = '';
    await refreshSources(true);
  } catch (error) { document.getElementById('sources-error').textContent = error.message; }
}

// ---- Keychain scrapers: one row per source, web sessions one row per service ----
const KC_LABEL = {ok: 'Authorized', need: 'Needs authorization', err: 'Action required', busy: 'Connecting…', wait: 'Waiting', off: 'Off'};
const KC_RANK = {off: 0, ok: 1, wait: 2, busy: 3, need: 4, err: 5};
const KC_BROWSERS = [['chrome', 'Chrome'], ['edge', 'Edge'], ['chromium', 'Chromium']];
let lastSourceReport = null, showOffApps = false, showOffWeb = false, kcPoll = null;

function kcEl(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function kcStatus(source, run) {
  if (!source.read) return 'off';
  if (source.revoked) return 'err';
  if (run.current.includes(source.id) || source.state === 'Connecting') return 'busy';
  if (run.queued.includes(source.id)) return 'wait';
  if (source.state === 'Action required') return 'err';
  if (source.state === 'Setup required') return 'need';
  return 'ok';
}

function kcChip(status, source) {
  const chip = kcEl('span', 'kc-chip kc-' + status, source && source.revoked ? 'Revoked' : KC_LABEL[status]);
  if (source && source.read && source.remote_paused) chip.textContent += ' · paused';
  if (source && status === 'need' && source.setup_requested) chip.textContent += ' · requested';
  if (source && source.state === 'Withdrawal pending') chip.title = 'Off · server withdrawal pending';
  return chip;
}

function kcSwitch(id, label, checked, disabled, mixed, onchange) {
  const wrap = kcEl('label', 'kc-switch' + (mixed ? ' mixed' : ''));
  const box = document.createElement('input');
  box.type = 'checkbox'; box.id = id; box.checked = checked; box.disabled = disabled; box.setAttribute('aria-label', label);
  box.onchange = () => onchange(box.checked);
  wrap.append(box, document.createElement('span'));
  return wrap;
}

function kcRow(toggle, name, chip) {
  const row = kcEl('div', 'kc-row');
  row.append(toggle, name, chip);
  return row;
}

function kcInfo(text) {
  const info = kcEl('span', 'kc-info', 'ⓘ');
  info.title = text; info.setAttribute('role', 'img'); info.setAttribute('aria-label', text);
  return info;
}

function kcBanner(cls, text, link) {
  const banner = kcEl('div', 'kc-banner ' + cls);
  banner.append(document.createTextNode(text));
  if (link) { const a = kcEl('a', '', link[0]); a.href = link[1]; banner.append(document.createTextNode(' '), a); }
  return banner;
}

function renderSources(report) {
  const card = document.getElementById('sources-card');
  // Keychain scrapers exist on macOS only; elsewhere there is nothing to show or to consent to.
  card.hidden = !report.compatible;
  const body = document.getElementById('sources-body');
  body.replaceChildren();
  if (!report.compatible) return;
  const run = report.run, selected = report.browsers;
  const live = report.sources.filter(s => s.compatible && (s.group === 'app' || selected[s.group]));
  const status = s => kcStatus(s, run);
  const blocked = report.re_pair_required || !report.forwarding_supported;

  if (report.re_pair_required) body.append(kcBanner('kc-b-err', 'Device authentication is unavailable, so Keychain credentials are withheld.', ['Re-pair this sidecar…', '/pair']));
  else if (!report.forwarding_supported) body.append(kcBanner('kc-b-warn', 'Your paired server does not support authenticated source controls yet, so Keychain credentials are withheld. You can still choose sources now.'));

  const fresh = live.filter(s => s.new);
  if (fresh.length) {
    const note = kcEl('div', 'kc-banner kc-b-new'); note.setAttribute('role', 'status');
    const dismiss = kcEl('button', '', 'Dismiss'); dismiss.type = 'button';
    dismiss.onclick = async () => { for (const s of fresh) await sourceAction('dismiss', s.id); };
    const names = new Set(fresh.map(s => s.service)).size;
    note.append(kcEl('span', '', names + ' new Keychain scraper' + (names > 1 ? 's' : '') + ' available — off until you opt in.'), dismiss);
    body.append(note);
  }

  const enabled = live.filter(s => s.read);
  const authorized = enabled.filter(s => status(s) === 'ok');
  const waiting = enabled.filter(s => status(s) === 'need');
  const failed = enabled.filter(s => status(s) === 'err');
  const summary = kcEl('div', 'kc-summary'); summary.setAttribute('aria-live', 'polite');
  const part = (cls, n, text) => { const span = kcEl('span', cls); span.append(kcEl('strong', '', String(n)), document.createTextNode(' ' + text)); return span; };
  summary.append(part('', enabled.length, 'of ' + live.length + ' on'), part('', authorized.length, 'authorized'));
  if (waiting.length) summary.append(part('kc-warn', waiting.length, 'need authorization'));
  if (failed.length) summary.append(part('kc-bad', failed.length, 'action required'));
  body.append(summary);

  // Apps and CLIs
  const apps = live.filter(s => s.group === 'app');
  const shownApps = apps.filter(s => s.read || showOffApps);
  const appGroup = kcEl('div', 'kc-group'), appRows = kcEl('div', 'kc-rows');
  appGroup.append(kcEl('div', 'kc-group-label', 'Apps & CLIs'), appRows);
  for (const s of shownApps) {
    const name = kcEl('span', 'kc-name', s.service);
    if (s.new) name.append(kcEl('span', 'kc-tag', 'NEW'));
    name.append(kcInfo(s.dependency));
    appRows.append(kcRow(kcSwitch('source-' + s.id, 'Use Keychain: ' + s.label, s.read, !!run.active, false, on => sourceAction('set_sources', null, on, {source_ids: [s.id]})), name, kcChip(status(s), s)));
  }
  const offApps = apps.length - apps.filter(s => s.read).length;
  if (offApps) {
    const more = kcEl('button', 'kc-more', showOffApps ? '▴ Hide sources that are off' : '▾ ' + offApps + ' more off (' + apps.filter(s => !s.read).map(s => s.service).join(', ') + ')');
    more.type = 'button'; more.onclick = () => { showOffApps = !showOffApps; renderSources(report); };
    appRows.append(more);
  }
  if (apps.length) body.append(appGroup);

  // Web sessions: one row per service, the browsers chosen above it
  const webGroup = kcEl('div', 'kc-group'), webRows = kcEl('div', 'kc-rows');
  const label = kcEl('div', 'kc-group-label'), picker = kcEl('span', 'kc-bsel');
  picker.title = 'Only the browsers you use are read; each one is one Keychain item';
  picker.append(document.createTextNode('Read from '));
  for (const [key, name] of KC_BROWSERS) {
    const choice = kcEl('label', selected[key] ? 'sel' : '');
    const box = document.createElement('input'); box.type = 'checkbox'; box.id = 'browser-' + key; box.checked = selected[key]; box.disabled = !!run.active;
    box.onchange = () => sourceAction('set_browsers', null, {[key]: box.checked});
    choice.append(box, document.createTextNode((selected[key] ? '✓ ' : '') + name));
    picker.append(choice);
  }
  label.append(kcEl('span', '', 'Web sessions (browser cookies)'), picker);
  webGroup.append(label, webRows);
  const services = new Map();
  for (const s of live) if (s.group !== 'app') { if (!services.has(s.service)) services.set(s.service, []); services.get(s.service).push(s); }
  let offServices = [];
  const browserOrder = key => KC_BROWSERS.findIndex(b => b[0] === key);
  for (const [service, children] of services) {
    children.sort((x, y) => browserOrder(x.group) - browserOrder(y.group));
    const ons = children.filter(s => s.read);
    if (!ons.length && !showOffWeb) { offServices.push(service); continue; }
    const mixed = ons.length > 0 && ons.length < children.length;
    const name = kcEl('span', 'kc-name', service);
    if (children.some(s => s.new)) name.append(kcEl('span', 'kc-tag', 'NEW'));
    for (const child of children) {
      const pill = kcEl('span', 'kc-pill'); const dot = kcEl('i', 'kc-d-' + status(child));
      pill.title = child.label + ': ' + KC_LABEL[status(child)];
      pill.append(dot, document.createTextNode(KC_BROWSERS.find(b => b[0] === child.group)[1]));
      name.append(pill);
    }
    const worst = children.map(status).reduce((a, b) => KC_RANK[b] > KC_RANK[a] ? b : a, 'off');
    webRows.append(kcRow(kcSwitch('service-' + service.replace(/[^A-Za-z0-9]+/g, '-'), 'Read ' + service + ' cookies', ons.length > 0, !!run.active, mixed, on => sourceAction('set_sources', null, on, {source_ids: children.map(c => c.id)})), name, kcChip(worst, children.length === 1 ? children[0] : null)));
  }
  if (offServices.length || (showOffWeb && services.size)) {
    const more = kcEl('button', 'kc-more', showOffWeb ? '▴ Hide services that are off' : '▾ ' + offServices.length + ' more off (' + offServices.join(', ') + ')');
    more.type = 'button'; more.onclick = () => { showOffWeb = !showOffWeb; renderSources(report); };
    webRows.append(more);
  }
  if (services.size) body.append(webGroup);

  // Claude Code renewal stays a per-source local choice
  const code = report.sources.find(s => s.id === 'anthropic.claude-code');
  if (code && code.compatible) {
    const renewal = kcEl('label', 'kc-renewal'), box = document.createElement('input');
    box.type = 'checkbox'; box.id = 'keychain-renewal'; box.checked = code.renewal_requested; box.disabled = !code.read;
    box.onchange = () => sourceAction('renewal', null, box.checked);
    const text = kcEl('span', '', 'Allow Claude Code Keychain renewal / write-back');
    text.append(document.createElement('br'), kcEl('span', 'field-hint', 'Also needs keep-alive and a verified compatible Keychain policy.'));
    renewal.append(box, text);
    body.append(renewal);
  }

  // Batch bar
  const batch = kcEl('div', 'kc-batch'), actions = kcEl('div', 'actions');
  const todo = enabled.filter(s => ['need', 'err'].includes(status(s)));
  const items = new Set(todo.map(s => s.item)).size;
  const button = (cls, text, onclick) => { const b = kcEl('button', 'btn ' + cls, text); b.type = 'button'; b.onclick = onclick; return b; };
  if (run.active) {
    actions.append(kcEl('button', 'btn btn-primary', 'Authorizing ' + Math.min(run.done + 1, run.total) + ' of ' + run.total + '…'));
    actions.firstChild.disabled = true;
    actions.append(button('btn-ghost', run.cancelled ? 'Stopping after this prompt…' : 'Cancel', () => sourceAction('authorize_cancel')));
  } else {
    if (todo.length) {
      const go = button('btn-primary', 'Authorize ' + todo.length + ' enabled · ' + items + ' Keychain item' + (items > 1 ? 's' : ''), () => sourceAction('authorize_all'));
      go.id = 'authorize-all';
      go.disabled = !report.setup_available || blocked;
      if (!report.setup_available) go.title = 'Authorization requires the packaged macOS app';
      else if (blocked) go.title = 'Keychain credentials are withheld until the issue above is fixed';
      actions.append(go);
    } else if (enabled.length && !failed.length) {
      const done = kcEl('span', '', '✓ Every enabled source is authorized'); done.style.cssText = 'font-size:.75rem;color:#86efac';
      actions.append(done);
    }
    const enableAll = button('btn-ghost', 'Enable all', () => sourceAction('enable_all'));
    enableAll.id = 'sources-enable-all'; enableAll.disabled = !report.compatible;
    actions.append(enableAll, button('btn-ghost', 'Disable all', () => sourceAction('disable_all')));
  }
  batch.append(actions);
  if (failed.length && !run.active) batch.append(kcEl('p', 'field-hint', failed.length + ' source' + (failed.length > 1 ? 's' : '') + ' cannot read the Keychain item yet (access blocked or revoked). Authorize again, or turn the source off and on to reset it.'));
  if (run.active) {
    const bar = kcEl('div', 'kc-progress'), fill = document.createElement('div');
    fill.style.width = (run.total ? run.done / run.total * 100 : 0) + '%'; bar.append(fill); batch.append(bar);
    batch.append(kcEl('p', 'field-hint', 'Approve the macOS Keychain prompt, then choose Always Allow so it does not ask again.'));
  }
  body.append(batch);
}

async function refreshSources(force) {
  const response = await fetch('/sources');
  if (!response.ok) return;
  const report = await response.json();
  const serialized = JSON.stringify(report);
  if (serialized !== lastSourceReport || force) {
    lastSourceReport = serialized;
    const focusedId = document.activeElement && document.activeElement.id;
    renderSources(report);
    if (focusedId) document.getElementById(focusedId)?.focus();
  }
  // A run is in flight: follow it closely, otherwise the slow poll is enough.
  // One timer at most: every refresh sees the run, so chaining a new one each time would multiply them.
  if (report.run && report.run.active && kcPoll === null) {
    kcPoll = setTimeout(() => { kcPoll = null; refreshSources().catch(() => {}); }, 1500);
  }
}
refreshSources().catch(() => {});
setInterval(() => refreshSources().catch(() => {}), 5000);

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

    def _valid_host(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        if self.headers.get("Host", "") != expected:
            self.send_error(403, "Forbidden")
            return False
        return True

    def do_GET(self) -> None:
        if not self._valid_host():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._serve_settings_page()
        elif path == "/pair":
            self._serve_pair_page()
        elif path == "/sources":
            from scripts.sidecar_pkg.source_settings import report

            self._send_json(report(self.server.get_config()))
        elif path == "/status":
            self._serve_status()
        elif path == "/logs":
            self._serve_logs()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if not self._valid_host():
            return
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
        if origin != allowed:
            self.send_error(403, "Forbidden")
            return

        path = urlparse(self.path).path
        if path == "/sources":
            self._handle_sources()
        elif path == "/save":
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
                lines = deque(f, maxlen=200)
            self._send_json({"lines": [line.rstrip() for line in lines]})
        except Exception as exc:
            self._send_json({"lines": [], "error": str(exc)})

    def _handle_sources(self) -> None:
        from scripts.sidecar_pkg.source_settings import update

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Invalid source request size")
            command = json.loads(self.rfile.read(length))
            if isinstance(command, dict) and command.get("action") == "authorize":
                from scripts.sidecar_pkg.source_catalog import catalog
                from scripts.sidecar_pkg.source_setup import SETUP

                source_id = command.get("source_id")
                if not isinstance(source_id, str) or source_id not in catalog():
                    raise ValueError("Unknown scraper")
                state = SETUP.start(source_id)
                self._send_json(
                    {"ok": state != "Off", "state": state}, 200 if state != "Off" else 409
                )
                return
            if isinstance(command, dict) and command.get("action") == "authorize_all":
                import sys

                from scripts.sidecar_pkg.source_catalog import catalog
                from scripts.sidecar_pkg.source_setup import SETUP

                # Every source that is on here and belongs to this platform; the controller
                # skips what is already authorized and groups what shares a Keychain item.
                ids = [i for i, d in catalog().items() if sys.platform in d.platforms]
                self._send_json({"ok": True, "run": SETUP.start_all(ids)})
                return
            if isinstance(command, dict) and command.get("action") == "authorize_cancel":
                from scripts.sidecar_pkg.source_setup import SETUP

                SETUP.cancel()
                self._send_json({"ok": True, "run": SETUP.run_report()})
                return
            updated = update(self.server.get_config(), command)
            self.server.save_config(updated)
        except (ValueError, TypeError):
            self._send_json({"ok": False, "error": "Invalid source action"}, 400)
            return
        except OSError:
            self._send_json({"ok": False, "error": "Could not save local source permissions"}, 500)
            return
        self._send_json({"ok": True})

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

        from scripts.sidecar_pkg.transport import ServerURLError, validate_server_url

        try:
            api_url = validate_server_url(api_url, base=True)
        except ServerURLError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        new_config = dict(self.server.get_config())
        new_config["api_url"] = api_url
        if api_key:
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
        for field in ("device_id", "device_key", "device_api_url"):
            new_config.pop(field, None)
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
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        # A hand-off body is one small JSON object. Refuse anything else
        # outright rather than reading part of it and leaving the rest unread.
        if not 0 <= length <= 8192:
            self.send_error(413 if length > 8192 else 400)
            return
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
    # SO_REUSEADDR permits multiple listeners on the same port on Windows.
    # os.name describes the running process at import, including PyInstaller builds.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

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
