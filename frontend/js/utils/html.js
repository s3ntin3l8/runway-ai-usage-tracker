// HTML-escape helpers. Previously this same pair lived inline in ~13 files;
// keep all callers pointed here so the escape rules stay consistent.

const _ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };

export function escapeHTML(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, m => _ESC_MAP[m]);
}

// Safe to embed in any HTML attribute, including a single-quoted JS string
// inside an onclick="..." handler. Two layers of escaping:
//
//   * HTML layer: `&`, `<`, `>`, `"` → HTML entities. `"` is the
//     load-bearing one — without it, user input containing `"` would close
//     the outer `attr="..."` and an attacker could inject new attributes
//     (e.g. onmouseover="alert(1)").
//   * JS-string layer: `'` → `\'` and `\` → `\\`. These survive the
//     browser's HTML-decode of the attribute value, so the JS parser sees
//     them as escapes inside the single-quoted string. HTML-encoding `'`
//     to &#039; would NOT survive — the browser decodes it back to `'`
//     before the JS parser runs, breaking out of the string.
//
// Side effect: a value containing `'` round-trips as `\'` when read back
// via `el.dataset.x`. Acceptable: provider/account/sidecar ids in this
// codebase don't legitimately contain apostrophes.
const _ATTR_ESC = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": "\\'",
    '\\': '\\\\',
};

export function escapeHTMLAttr(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"'\\]/g, m => _ATTR_ESC[m]);
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
