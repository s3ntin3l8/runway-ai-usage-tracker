// Tests for frontend/js/views/history_cache.js
//
// Runs via `node --test tests/frontend/test_history_cache.mjs`. Picked up
// transitively by tests/unit/test_frontend_escapes.py which globs every
// .mjs file in tests/frontend/.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CACHE_TTL_MS,
    _cacheKey,
    _cacheHit,
    _filterChartByDays,
    _filterSnapshotsByDays,
} from '../../frontend/js/views/history_cache.js';

// ---------------------------------------------------------------------------
// _cacheKey
// ---------------------------------------------------------------------------

test('_cacheKey is stable across calls', () => {
    const a = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: 'anthropic,openai' });
    const b = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: 'anthropic,openai' });
    assert.equal(a, b);
});

test('_cacheKey distinguishes metric', () => {
    const a = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: '*' });
    const b = _cacheKey({ metric: 'tokens',  windowFilter: 'all', providers: '*' });
    assert.notEqual(a, b);
});

test('_cacheKey distinguishes windowFilter', () => {
    const a = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: '*' });
    const b = _cacheKey({ metric: 'percent', windowFilter: 'session', providers: '*' });
    assert.notEqual(a, b);
});

test('_cacheKey distinguishes provider sets', () => {
    const all = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: '*' });
    const claude = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: 'anthropic' });
    const both = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: 'anthropic,openai' });
    const none = _cacheKey({ metric: 'percent', windowFilter: 'all', providers: 'none' });
    assert.notEqual(all, claude);
    assert.notEqual(claude, both);
    assert.notEqual(all, none);
});

// ---------------------------------------------------------------------------
// _cacheHit
// ---------------------------------------------------------------------------

test('_cacheHit returns null on miss', () => {
    assert.equal(_cacheHit({}, 'percent|all|*', 7), null);
});

test('_cacheHit returns null when stale', () => {
    const slot = { 'percent|all|*': { fetchedAt: 0, days: 30, response: {} } };
    const now = CACHE_TTL_MS + 1;
    assert.equal(_cacheHit(slot, 'percent|all|*', 7, now), null);
});

test('_cacheHit returns null when cached.days < requested.days', () => {
    const slot = { 'percent|all|*': { fetchedAt: 100, days: 1, response: {} } };
    assert.equal(_cacheHit(slot, 'percent|all|*', 7, 200), null);
});

test('_cacheHit returns entry when fresh and cached.days >= requested.days', () => {
    const entry = { fetchedAt: 100, days: 30, response: { series: [] } };
    const slot = { 'percent|all|*': entry };
    assert.equal(_cacheHit(slot, 'percent|all|*', 7, 200), entry);
});

test('_cacheHit returns entry on exact-days match', () => {
    const entry = { fetchedAt: 100, days: 7, response: {} };
    const slot = { 'percent|all|*': entry };
    assert.equal(_cacheHit(slot, 'percent|all|*', 7, 200), entry);
});

// ---------------------------------------------------------------------------
// _filterChartByDays
// ---------------------------------------------------------------------------

test('_filterChartByDays filters series.points by cutoff', () => {
    const now = Date.parse('2026-05-19T12:00:00Z');
    const response = {
        series: [
            {
                provider_id: 'anthropic',
                points: [
                    { ts: '2026-05-12T12:00:00Z', pct_used: 10 },  // 7 days old (boundary)
                    { ts: '2026-05-15T12:00:00Z', pct_used: 20 },  // within 1 day cutoff? no
                    { ts: '2026-05-19T11:00:00Z', pct_used: 30 },  // within 1 day cutoff
                ],
            },
        ],
    };
    const filtered = _filterChartByDays(response, 1, now);
    assert.deepEqual(filtered.series[0].points.map(p => p.pct_used), [30]);
});

test('_filterChartByDays preserves series-level fields', () => {
    const now = Date.parse('2026-05-19T12:00:00Z');
    const response = {
        series: [{ provider_id: 'openai', label: 'OpenAI', points: [{ ts: '2026-05-19T11:00:00Z', pct_used: 5 }] }],
    };
    const filtered = _filterChartByDays(response, 1, now);
    assert.equal(filtered.series[0].provider_id, 'openai');
    assert.equal(filtered.series[0].label, 'OpenAI');
});

test('_filterChartByDays filters bars by cutoff', () => {
    const now = Date.parse('2026-05-19T12:00:00Z');
    const response = {
        bars: [
            { ts: '2026-05-10T00:00:00Z', segments: [{ value: 1 }] },
            { ts: '2026-05-19T11:00:00Z', segments: [{ value: 2 }] },
        ],
    };
    const filtered = _filterChartByDays(response, 1, now);
    assert.equal(filtered.bars.length, 1);
    assert.equal(filtered.bars[0].segments[0].value, 2);
});

// ---------------------------------------------------------------------------
// _filterSnapshotsByDays
// ---------------------------------------------------------------------------

test('_filterSnapshotsByDays filters rows; preserves total/page/limit', () => {
    const now = Date.parse('2026-05-19T12:00:00Z');
    const response = {
        total: 100,
        page: 1,
        limit: 50,
        rows: [
            { ts: '2026-05-10T00:00:00Z', pct_used: 10 },
            { ts: '2026-05-19T11:00:00Z', pct_used: 20 },
        ],
    };
    const filtered = _filterSnapshotsByDays(response, 1, now);
    assert.equal(filtered.total, 100);
    assert.equal(filtered.page, 1);
    assert.equal(filtered.limit, 50);
    assert.equal(filtered.rows.length, 1);
    assert.equal(filtered.rows[0].pct_used, 20);
});

test('_filterSnapshotsByDays does not mutate the original response', () => {
    const now = Date.parse('2026-05-19T12:00:00Z');
    const response = {
        total: 2,
        page: 1,
        limit: 50,
        rows: [
            { ts: '2026-05-10T00:00:00Z' },
            { ts: '2026-05-19T11:00:00Z' },
        ],
    };
    _filterSnapshotsByDays(response, 1, now);
    assert.equal(response.rows.length, 2);
});
