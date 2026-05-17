// HTML-escape helpers. Previously this same pair lived inline in ~13 files;
// keep all callers pointed here so the escape rules stay consistent.

const _ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };

export function escapeHTML(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, m => _ESC_MAP[m]);
}

// For values being interpolated into single-quoted attributes built inside
// JS-emitted onclick="..." handlers. Escapes backslashes and apostrophes.
export function escapeHTMLAttr(str) {
    if (!str) return '';
    return String(str).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

// For values rendered into href/src attributes. Rejects non-http(s) schemes
// (javascript:, data:, vbscript:) so attacker-controlled URLs from sidecar
// payloads can't escalate to script execution. Returns '' on bad input so
// callers can short-circuit and omit the element entirely.
export function safeUrl(url) {
    if (!url) return '';
    try {
        const parsed = new URL(url, window.location.origin);
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
        return escapeHTML(parsed.href);
    } catch {
        return '';
    }
}
