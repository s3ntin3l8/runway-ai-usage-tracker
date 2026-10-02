import { describe, expect, it } from 'vitest';
import { getSourceSplitConfig } from './sourceSplitConfig';

const byModel = { sonnet: { tokens_output: 20 } };
const bySidecar = {
  laptop: { tokens_output: 10 },
  desktop: { tokens_output: 10 },
};
const aggregation = {
  window_type: 'weekly',
  window_start: '2026-09-01T00:00:00Z',
  window_end: '2026-09-08T00:00:00Z',
  token_usage: {},
  by_model: byModel,
  by_sidecar: bySidecar,
};

describe('getSourceSplitConfig', () => {
  it('uses sidecars for a multi-source active quota window', () => {
    const result = getSourceSplitConfig({
      kind: 'quota',
      aggregation,
      scopeBucket: null,
      scopeLabel: 'Last 7 days',
    });

    expect(result).toMatchObject({
      title: 'Current window by source',
      split: bySidecar,
      useWindowSplit: true,
      windowType: 'weekly',
      hasSourceSplit: true,
    });
  });

  it('limits a quota-window split to the selected source', () => {
    const result = getSourceSplitConfig({
      kind: 'quota',
      sidecarId: 'laptop',
      aggregation,
      scopeBucket: null,
      scopeLabel: 'Last 7 days',
    });

    expect(result.split).toEqual({ laptop: bySidecar.laptop });
  });

  it('shows the current-window empty state when the selected sidecar has no activity', () => {
    const result = getSourceSplitConfig({
      kind: 'quota',
      sidecarId: 'phone',
      aggregation,
      scopeBucket: null,
      scopeLabel: 'Last 7 days',
    });

    expect(result).toMatchObject({
      title: 'Current window by source',
      split: {},
      useWindowSplit: true,
      hasSourceSplit: false,
    });
  });

  it('uses the selected-period model split for non-quota providers', () => {
    const bucket = { by_model: byModel };
    const result = getSourceSplitConfig({
      kind: 'tokens',
      scopeBucket: bucket,
      scopeLabel: 'March 2026',
    });

    expect(result).toMatchObject({
      title: 'Tokens by model · March 2026',
      split: byModel,
      useWindowSplit: false,
    });
  });
});
