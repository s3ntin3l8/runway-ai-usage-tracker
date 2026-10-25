// The segment colour must belong to the SEGMENT, not to its position in the
// visible list: toggling "Exclude cache" shortens the list, and a positional
// palette would renumber the colours under the user.
//
// This file pins that contract across the two components that paint the same
// segments: the CSS bar (TokenBar) and the ECharts donut (TokenDonut).

import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { CumulativeModelBucket } from '@/api/types';
import { TOKEN_SEGMENT_KEYS } from './segmentDefs';
import { SEGMENT_COLOR_SLOT } from './segmentColors';
import type { TokenSliceKey } from './tokenSlices';

const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" />;
  },
}));

import { TokenBar } from './TokenBar';
import { TokenDonut } from './TokenDonut';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

// jsdom resolves CSS custom properties to '' unless they are set.
const PALETTE = ['#c1', '#c2', '#c3', '#c4', '#c5', '#c6'];

const TOKENS = {
  tokens_input: 100,
  tokens_output: 200,
  tokens_cache_read: 300,
  tokens_cache_create: 400,
  tokens_reasoning: 500,
};

/** The `--chart-N` slot the bar paints a segment with. */
function barSlots(container: HTMLElement): Map<string, number> {
  const slots = new Map<string, number>();
  for (const el of container.querySelectorAll<HTMLElement>('[title]')) {
    const label = el.getAttribute('title')?.split(':')[0] ?? '';
    const slot = Number(el.style.background.match(/--chart-(\d+)/)?.[1]);
    slots.set(label, slot);
  }
  return slots;
}

/** The `--chart-N` slot the donut paints a slice with. */
function donutSlots(): Map<string, number> {
  const data = (captured.option!.series as { data: { name: string; itemStyle?: { color: string } }[] }[])[0]
    .data;
  const slots = new Map<string, number>();
  for (const slice of data) {
    slots.set(slice.name, PALETTE.indexOf(slice.itemStyle?.color ?? '') + 1);
  }
  return slots;
}

describe('segment colours', () => {
  beforeEach(() => {
    PALETTE.forEach((c, i) => document.documentElement.style.setProperty(`--chart-${i + 1}`, c));
  });

  afterEach(() => {
    PALETTE.forEach((_, i) =>
      document.documentElement.style.removeProperty(`--chart-${i + 1}`),
    );
  });

  it('follows the canonical segment order', () => {
    expect(
      TOKEN_SEGMENT_KEYS.map((k) => SEGMENT_COLOR_SLOT[k.key as TokenSliceKey]),
    ).toEqual([1, 2, 3, 4, 5]);
  });

  it('gives the bar and the donut the same colour for every segment', () => {
    const { container } = render(<TokenBar tokens={TOKENS} />);
    render(<TokenDonut bucket={TOKENS as CumulativeModelBucket} />, { wrapper });
    const bar = barSlots(container);
    const donut = donutSlots();
    expect([...bar.keys()].sort()).toEqual([...donut.keys()].sort());
    for (const [label, slot] of bar) {
      expect(donut.get(label)).toBe(slot);
    }
  });

  it('keeps a segment colour when cache is excluded', () => {
    const { container: barWithCache } = render(<TokenBar tokens={TOKENS} />);
    render(<TokenDonut bucket={TOKENS as CumulativeModelBucket} />, { wrapper });
    const donutWithCache = donutSlots();

    const { container: barWithoutCache } = render(<TokenBar tokens={TOKENS} excludeCache />);
    render(<TokenDonut bucket={TOKENS as CumulativeModelBucket} excludeCache />, { wrapper });
    const donutWithoutCache = donutSlots();

    // Excluding cache removes two segments; the rest must not be renumbered.
    for (const label of ['Input', 'Output', 'Reasoning']) {
      expect(barSlots(barWithoutCache).get(label)).toBe(barSlots(barWithCache).get(label));
      expect(donutWithoutCache.get(label)).toBe(donutWithCache.get(label));
    }
    // ...and the bar and the donut still agree with each other.
    for (const label of ['Input', 'Output', 'Reasoning']) {
      expect(donutWithoutCache.get(label)).toBe(barSlots(barWithoutCache).get(label));
    }
  });
});
