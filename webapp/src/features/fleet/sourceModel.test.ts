import type { FleetSourceStatus } from '@/api/types';
import {
  countSources,
  groupSources,
  isShown,
  macStatus,
  matches,
  needsAuthorization,
  rollup,
  serviceLabel,
  sourceBrowser,
} from './sourceModel';

const src = (source_id: string, over: Partial<FleetSourceStatus> = {}): FleetSourceStatus => ({
  source_id,
  provider_id: source_id.split('.')[0],
  label: source_id,
  revision: 'r',
  generation: 1,
  read: true,
  state: 'Ready',
  renewal_requested: false,
  renewal_supported: false,
  renewal_capable: false,
  new: false,
  remote_paused: false,
  revoked: false,
  report_at: '2026-10-29T10:00:00Z',
  report_stale: false,
  announcement: false,
  collection_health: 'healthy',
  last_collection_at: null,
  ...over,
});

describe('macStatus', () => {
  it.each([
    [{ read: false, state: 'Off' as const }, 'off'],
    [{ read: false, state: 'Action required' as const }, 'off'],
    [{ state: 'Ready' as const }, 'ok'],
    [{ state: 'Limited' as const }, 'ok'],
    [{ state: 'Setup required' as const }, 'need'],
    [{ state: 'Off' as const }, 'need'],
    [{ state: 'Connecting' as const }, 'busy'],
    [{ state: 'Action required' as const }, 'err'],
    [{ state: 'Ready' as const, revoked: true }, 'err'],
    [{ state: 'Withdrawal pending' as const }, 'ok'],
  ])('%j -> %s', (over, expected) => {
    expect(macStatus(src('a.b', over))).toBe(expected);
  });
});

describe('naming', () => {
  it('reads the browser from the source id, and none for an app', () => {
    expect(sourceBrowser(src('anthropic.chrome'))).toBe('Chrome');
    expect(sourceBrowser(src('anthropic.edge'))).toBe('Edge');
    expect(sourceBrowser(src('anthropic.chromium'))).toBe('Chromium');
    expect(sourceBrowser(src('anthropic.desktop-session'))).toBeNull();
    expect(sourceBrowser(src('github.gh-cli'))).toBeNull();
  });

  it('strips the browser from a cookie source label and leaves other labels alone', () => {
    expect(serviceLabel(src('a.chrome', { label: 'Claude Pro · Chrome cookies' }))).toBe('Claude Pro');
    expect(serviceLabel(src('a.edge', { label: 'Claude Console · Edge cookies' }))).toBe('Claude Console');
    expect(serviceLabel(src('a.x', { label: 'Claude Code' }))).toBe('Claude Code');
  });
});

describe('filters', () => {
  const sources = [
    src('a.one'), // on, collecting, ok
    src('a.two', { state: 'Setup required', collection_health: 'untried' }), // on, needs authorization
    src('a.three', { collection_health: 'action_required' }), // on, ok, server failing
    src('a.four', { state: 'Action required', collection_health: 'untried' }), // on, err
    src('a.five', { read: false, state: 'Off', collection_health: 'untried' }), // off
    src('a.six', { read: false, state: 'Setup required', collection_health: 'healthy' }), // off, never counted elsewhere
  ];

  it('counts sources over the same predicate the list uses', () => {
    const counts = countSources(sources);
    expect(counts).toEqual({ total: 6, on: 4, collecting: 1, attention: 3, off: 2 });
    for (const filter of ['on', 'collecting', 'attention', 'off'] as const)
      expect(sources.filter((s) => isShown(s, filter, false))).toHaveLength(counts[filter]);
  });

  it('collecting and attention need the source to be on', () => {
    const six = sources[5];
    expect(matches('collecting', six)).toBe(false);
    expect(matches('attention', six)).toBe(false);
    expect(matches('off', six)).toBe(true);
  });

  it('shows what is on, plus what is off when the fold is open, unless filtering', () => {
    const five = sources[4];
    expect(isShown(five, null, false)).toBe(false);
    expect(isShown(five, null, true)).toBe(true);
    expect(isShown(five, 'on', true)).toBe(false);
    expect(isShown(sources[0], 'off', true)).toBe(false);
  });

  it('knows which sources still need authorization on the device', () => {
    expect(sources.filter(needsAuthorization).map((s) => s.source_id)).toEqual(['a.two', 'a.four']);
  });
});

describe('grouping', () => {
  it('keeps apps as single rows and collapses cookie sources into one group per service, in order', () => {
    const list = [
      src('anthropic.claude-code'),
      src('anthropic.chrome', { label: 'Claude Pro · Chrome cookies' }),
      src('chatgpt.chrome', { label: 'ChatGPT Codex · Chrome cookies' }),
      src('anthropic.edge', { label: 'Claude Pro · Edge cookies' }),
      src('github.gh-cli'),
    ];
    const { apps, services } = groupSources(list);
    expect(apps.map((s) => s.source_id)).toEqual(['anthropic.claude-code', 'github.gh-cli']);
    expect(services.map((g) => [g.label, g.sources.map((s) => s.source_id)])).toEqual([
      ['Claude Pro', ['anthropic.chrome', 'anthropic.edge']],
      ['ChatGPT Codex', ['chatgpt.chrome']],
    ]);
  });

  it('rolls up to the worst status, and to off only when everything is off', () => {
    const off = src('a.chrome', { read: false });
    expect(rollup([off, off])).toBe('off');
    expect(rollup([off, src('a.edge')])).toBe('ok');
    expect(rollup([src('a.edge'), src('a.chrome', { state: 'Setup required' })])).toBe('need');
    expect(rollup([src('a.edge', { state: 'Connecting' }), src('a.chrome', { state: 'Setup required' })])).toBe('need');
    expect(rollup([src('a.chrome', { state: 'Setup required' }), src('a.edge', { state: 'Action required' })])).toBe('err');
    expect(rollup([])).toBe('off');
  });
});
