/**
 * Debug pane builder for the provider detail modal.
 *
 * Data sources:
 *   - entry      : fleet entry (provider_id, account_id, critical_gauge, sidecar_contributions)
 *   - tokenHealth: response from /api/v1/system/token-health (may be null)
 */

import { providerDisplayLabel } from '../../components.js';
import { formatLocalDateTime } from '../../utils/tz.js';

function _esc(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function _fmtAgo(isoStr) {
    if (!isoStr) return '—';
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (diff < 60)    return Math.round(diff) + 's';
    if (diff < 3600)  return Math.round(diff / 60) + 'm';
    if (diff < 86400) return Math.round(diff / 3600) + 'h';
    return Math.round(diff / 86400) + 'd';
}

function _fmtUntil(isoStr) {
    if (!isoStr) return '—';
    const diff = (new Date(isoStr).getTime() - Date.now()) / 1000;
    if (diff <= 0)    return 'now';
    if (diff < 60)    return Math.round(diff) + 's';
    if (diff < 3600)  return Math.round(diff / 60) + 'm';
    if (diff < 86400) return (diff / 3600).toFixed(1) + 'h';
    return Math.round(diff / 86400) + 'd';
}

function _osGlyph(os) {
    if (!os) return '◈ ';
    const l = os.toLowerCase();
    if (l.includes('darwin') || l.includes('mac')) return '⌘ ';
    if (l.includes('win')) return '⊞ ';
    if (l.includes('linux')) return '🐧 ';
    return '◈ ';
}

function _scHue(id, idx) {
    let hash = 0;
    for (const ch of String(id)) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
    return Math.abs(hash) % 360 || (idx * 47 + 60) % 360;
}

/** Build a key-value row for the authoritative source table. */
function _kvRow(key, val, cls) {
    const cls_ = cls ? ` ${cls}` : '';
    return `<div class="m-event${cls_}">
        <span class="t">${_esc(key)}</span>
        <span class="dot" style="opacity:.4"></span>
        <span class="msg">${_esc(val)}</span>
        <span class="v"></span>
    </div>`;
}

/**
 * Build the Debug pane HTML string.
 *
 * @param {object} entry - Fleet entry from STATE.fleet
 * @param {object|null} tokenHealth - Full /system/token-health response
 * @returns {string} HTML string
 */
export function buildDebugPane(entry, tokenHealth) {
    const critical = entry.critical_gauge || {};
    const providerId  = entry.provider_id || '';
    const accountId   = entry.account_id || '';
    const provLabel   = providerDisplayLabel(providerId) || providerId;
    const accountLabel = critical.account_label || accountId || 'default';
    const plan        = critical.tier || critical.plan || '—';
    const windowType  = critical.window_type || '—';
    const kind        = critical.is_unlimited ? 'payg / unlimited' : (critical.pct_used != null ? 'quota' : 'enrichment');
    const dataSource  = critical.data_source || '—';
    const inputSource = critical.input_source || '—';
    const sourceStr   = dataSource === 'api' ? 'OAuth · /v1/limits' : dataSource === 'web' ? 'web · scraped' : dataSource === 'local' ? 'local · sidecar' : dataSource;
    const lastPolled  = critical.fetched_at ? _fmtAgo(critical.fetched_at) + ' ago' : '—';
    const nextPoll    = critical.next_poll_at ? 'in ' + _fmtUntil(critical.next_poll_at) : '—';
    const ttl         = critical.cache_ttl_seconds ? `${critical.cache_ttl_seconds}s · cached` : '—';

    // Authoritative source block
    const authRows = [
        ['provider',  provLabel],
        ['account',   accountLabel],
        ['plan',      plan],
        ['window',    windowType],
        ['kind',      kind],
        ['source',    sourceStr],
        ['data_src',  dataSource],
        ['input_src', inputSource],
        ['ttl',       ttl],
        ['last poll', lastPolled],
        ['next poll', nextPoll],
    ].map(([k, v]) => _kvRow(k, v)).join('');

    // Sidecar list
    const contributions = entry.sidecar_contributions || {};
    const sidecarEntries = Object.entries(contributions);
    const sidecarCount = sidecarEntries.length;

    const sidecarRowsHtml = sidecarEntries.length
        ? sidecarEntries.map(([sid, stats], i) => {
            const hue = _scHue(sid, i);
            const os = stats.os || '—';
            const status = stats.status || 'active';
            const age = stats.last_seen ? _fmtAgo(stats.last_seen) : '—';
            const tokens = (stats.tokens_input || 0) + (stats.tokens_output || 0);
            const delta = tokens > 0 ? `+${(tokens / 1e6).toFixed(2)}M tok` : '—';
            return `<div class="m-side-row" style="grid-template-columns: 14px 1fr 80px 80px 80px 80px">
                <span class="swatch" style="background: oklch(0.62 0.15 ${hue})"></span>
                <span class="nm"><span class="os">${_osGlyph(os)}</span>${_esc(sid)}</span>
                <span class="cost">${_esc(os)}</span>
                <span class="cost">${_esc(status)}</span>
                <span class="cost">${_esc(age)}</span>
                <span class="delta">${_esc(delta)}</span>
            </div>`;
        }).join('')
        : `<div class="m-event"><span class="t">—</span><span class="dot"></span><span class="msg">No sidecars registered</span><span class="v"></span></div>`;

    // Token health — filter to this provider+account
    let tokenHealthHtml = '';
    if (tokenHealth && tokenHealth.tokens && tokenHealth.tokens.length) {
        const myTokens = tokenHealth.tokens.filter(t =>
            !t.provider || t.provider === providerId
        );
        if (myTokens.length) {
            tokenHealthHtml = myTokens.map(t => {
                const expiresIn = t.expires_at ? _fmtUntil(t.expires_at) + ' remaining' : 'unknown expiry';
                const issueDate = formatLocalDateTime(t.issued_at, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
                const scopes = t.scopes ? t.scopes.join(' · ') : '—';
                const healthCls = t.health === 'good' ? 'good' : t.health === 'warn' ? 'warn' : '';
                return `<div class="m-event ${healthCls}">
                    <span class="t">expires</span><span class="dot"></span>
                    <span class="msg">OAuth bearer · ${_esc(expiresIn)}</span>
                    <span class="v">${t.auto_rotate ? 'auto-rotate on' : ''}</span>
                </div>
                <div class="m-event">
                    <span class="t">scope</span><span class="dot" style="opacity:.4"></span>
                    <span class="msg">${_esc(scopes)}</span>
                    <span class="v">verified</span>
                </div>
                <div class="m-event">
                    <span class="t">issued</span><span class="dot" style="opacity:.4"></span>
                    <span class="msg">${_esc(issueDate)}</span>
                    <span class="v"></span>
                </div>`;
            }).join('');
        }
    }

    if (!tokenHealthHtml) {
        tokenHealthHtml = `<div class="m-event">
            <span class="t">status</span><span class="dot" style="opacity:.4"></span>
            <span class="msg">No token data available for this account</span>
            <span class="v"></span>
        </div>`;
    }

    return `
    <!-- AUTHORITATIVE SOURCE -->
    <div class="m-block m-raw-block">
        <div class="head">
            <h4>Authoritative source</h4>
            <span class="meta">${_esc(lastPolled)}</span>
        </div>
        <div class="m-events">
            ${authRows}
        </div>
    </div>

    <!-- SIDECARS -->
    <div class="m-block">
        <div class="head">
            <h4>Sidecars</h4>
            <span class="meta">${sidecarCount} active</span>
        </div>
        <div class="m-side">
            ${sidecarRowsHtml}
        </div>
    </div>

    <!-- TOKEN HEALTH -->
    <div class="m-block m-raw-block">
        <div class="head"><h4>Token health</h4></div>
        <div class="m-events">
            ${tokenHealthHtml}
        </div>
    </div>
    `;
}
