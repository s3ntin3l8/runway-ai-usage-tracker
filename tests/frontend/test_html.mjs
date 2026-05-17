// Tests for frontend/js/utils/html.js
//
// Runs via `node --test tests/frontend/test_html.mjs`. Wrapped by
// tests/unit/test_frontend_escapes.py so pytest picks it up too.

import test from 'node:test';
import assert from 'node:assert/strict';

import { escapeHTML, escapeHTMLAttr, safeUrl } from '../../frontend/js/utils/html.js';

// ---------------------------------------------------------------------------
// escapeHTML — used for HTML body text and double-quoted attribute values.
// ---------------------------------------------------------------------------

test('escapeHTML escapes the five HTML metacharacters', () => {
    assert.equal(escapeHTML('&<>"\''), '&amp;&lt;&gt;&quot;&#039;');
});

test('escapeHTML coerces non-strings', () => {
    assert.equal(escapeHTML(42), '42');
});

test('escapeHTML returns empty for falsy', () => {
    assert.equal(escapeHTML(''), '');
    assert.equal(escapeHTML(null), '');
    assert.equal(escapeHTML(undefined), '');
});

// ---------------------------------------------------------------------------
// escapeHTMLAttr — used for values interpolated into HTML attributes,
// including single-quoted JS strings inside onclick="..." handlers. Must
// be safe in BOTH contexts: the browser HTML-decodes the attribute value
// before the JS parser sees it, so the layered escape has to survive both.
// ---------------------------------------------------------------------------

test('escapeHTMLAttr escapes double-quote so it cannot close the outer HTML attribute', () => {
    // Regression for the audit-flagged XSS surface: callers wrap the result
    // in onclick="window.fn('${escapeHTMLAttr(v)}', ...)". If `"` were not
    // escaped, the literal `"` would close the `onclick="..."` attribute.
    const out = escapeHTMLAttr('foo"onmouseover="alert(1)"x');
    assert.ok(!out.includes('"'), `expected no literal " in ${JSON.stringify(out)}`);
    assert.equal(out, 'foo&quot;onmouseover=&quot;alert(1)&quot;x');
});

test('escapeHTMLAttr escapes ampersand first so further escapes survive HTML decoding', () => {
    // If `&` weren't escaped, a literal `&quot;` in user input would be
    // HTML-decoded to `"` by the browser and become an attribute-breakout.
    assert.equal(escapeHTMLAttr('a&b'), 'a&amp;b');
    assert.equal(escapeHTMLAttr('&quot;'), '&amp;quot;');
});

test('escapeHTMLAttr escapes apostrophe with JS-level escape (not HTML entity)', () => {
    // `'` must survive HTML decoding to still close the JS string in
    // onclick="...'${v}'". HTML-encoding `'` to &#039; would be decoded
    // back to `'` by the browser before the JS parser runs — unsafe.
    assert.equal(escapeHTMLAttr("can't"), "can\\'t");
});

test('escapeHTMLAttr escapes backslash so JS-string escape sequences are literal', () => {
    assert.equal(escapeHTMLAttr('a\\b'), 'a\\\\b');
    // Combined: backslash and apostrophe both present
    assert.equal(escapeHTMLAttr("a\\'b"), "a\\\\\\'b");
});

test('escapeHTMLAttr escapes angle brackets for defence in depth', () => {
    assert.equal(escapeHTMLAttr('<script>'), '&lt;script&gt;');
});

test('escapeHTMLAttr is idempotent on plain identifiers', () => {
    // The common case: provider IDs / account IDs / sidecar IDs.
    assert.equal(escapeHTMLAttr('anthropic'), 'anthropic');
    assert.equal(escapeHTMLAttr('user@example.com'), 'user@example.com');
});

test('escapeHTMLAttr returns empty for falsy', () => {
    assert.equal(escapeHTMLAttr(''), '');
    assert.equal(escapeHTMLAttr(null), '');
    assert.equal(escapeHTMLAttr(undefined), '');
});

// ---------------------------------------------------------------------------
// safeUrl — gates non-http(s) schemes.
// ---------------------------------------------------------------------------

test('safeUrl rejects javascript:, data:, vbscript: schemes', () => {
    // safeUrl uses `new URL(url, window.location.origin)`. window isn't defined
    // in node:test, but the URL constructor short-circuits before touching
    // window when an absolute URL is passed. The dangerous schemes are flat
    // rejected; we only need a base-URL for relative paths.
    // Use a stub global so the helper finds window.location.origin.
    if (typeof globalThis.window === 'undefined') {
        globalThis.window = { location: { origin: 'http://example.test' } };
    }
    assert.equal(safeUrl('javascript:alert(1)'), '');
    assert.equal(safeUrl('data:text/html,<script>'), '');
    assert.equal(safeUrl('vbscript:msgbox'), '');
});

test('safeUrl accepts http(s) and HTML-escapes the result', () => {
    if (typeof globalThis.window === 'undefined') {
        globalThis.window = { location: { origin: 'http://example.test' } };
    }
    assert.equal(safeUrl('https://example.com/a?b=1'), 'https://example.com/a?b=1');
});
