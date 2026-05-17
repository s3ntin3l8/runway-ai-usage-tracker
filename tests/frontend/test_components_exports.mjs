// Smoke test: every name external callers import from components.js must
// remain exported, even after the R11 monolith split. Catches a regression
// where a refactor inadvertently drops or renames a public symbol — the
// frontend would otherwise only fail at runtime in the browser.

import test from 'node:test';
import assert from 'node:assert/strict';

// Minimal DOM/Storage stubs so the module's top-level code can execute.
globalThis.localStorage = { getItem: () => null, setItem: () => null, removeItem: () => null };
globalThis.window = {
    location: { origin: 'http://example.test' },
    addEventListener: () => null,
    matchMedia: () => ({ matches: false, addEventListener: () => null }),
};
globalThis.document = { addEventListener: () => null, querySelectorAll: () => [] };

const REQUIRED_EXPORTS = [
    // Imported by frontend/js/app.js
    'buildGitHubOAuthModal',
    'buildFleetView',
    'buildTokenHealthPanel',
    'escapeHTMLAttr',
    'buildProviderSparklineStrip',
    // Imported by frontend/js/views/dashboard.js
    'buildHorizonCard',
    'buildCardModalContent',
    'providerDisplayLabel',
    'buildFleetCommanderCard',
    // Imported by frontend/js/views/modal/{index,debug,overview}.js
    // (providerDisplayLabel covered above)
    // Imported by frontend/js/views/history.js
    // (buildProviderSparklineStrip covered above)
    // Imported by frontend/js/views/fleet.js
    // (buildFleetView covered above)
    // Other publicly-used builders kept exported by the original module
    'buildModalSkeleton',
    'buildProviderModal',
    'buildModalContent',
];

test('components.js continues to export every name external callers consume', async () => {
    const mod = await import('../../frontend/js/components.js');
    const missing = REQUIRED_EXPORTS.filter(name => !(name in mod));
    assert.deepEqual(missing, [], `missing exports: ${missing.join(', ')}`);
});
