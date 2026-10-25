import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { HeatmapCell } from '@/api/types';

const captured: { option?: Record<string, unknown>; className?: string } = {};
vi.mock('./EChart', () => ({
  // The mock forwards ariaLabel the way the real EChart does, so the tests
  // can assert that each chart hands its label down.
  EChart: ({
    option,
    className,
    ariaLabel,
  }: {
    option: Record<string, unknown>;
    className?: string;
    ariaLabel?: string;
  }) => {
    captured.option = option;
    captured.className = className;
    return (
      <div
        data-testid="echart"
        role={ariaLabel ? 'img' : undefined}
        aria-label={ariaLabel}
      />
    );
  },
}));

import { UsageHeatmap } from './UsageHeatmap';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const HOURS = Array.from({ length: 24 }, (_, h) => String(h));
const PADDED_HOURS = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0'));

type TooltipOption = {
  formatter: (p: { value: [number, number, number, number] }) => string;
};

// The global test setup installs a matchMedia that never matches, so every
// test that cares about the layout installs its own (see useMediaQuery.test.tsx).
const originalMatchMedia = window.matchMedia;
function installMatchMedia(matches: boolean) {
  const mql = {
    matches,
    media: '',
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia;
}

describe('UsageHeatmap', () => {
  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('maps cells to [hour, dow, sqrt(tokens), tokens] tuples and a sqrt-scaled visualMap max', () => {
    installMatchMedia(true); // desktop
    const cells: HeatmapCell[] = [
      { hour: 9, dow: 1, tokens: 100 },
      { hour: 14, dow: 3, tokens: 4 },
    ];
    render(<UsageHeatmap cells={cells} />, { wrapper });
    const opt = captured.option!;
    const series = opt.series as { data: number[][] }[];
    expect(series[0].data).toEqual([
      [9, 1, 10, 100],
      [14, 3, 2, 4],
    ]);
    const visualMap = opt.visualMap as { max: number; dimension: number };
    expect(visualMap.dimension).toBe(2);
    expect(visualMap.max).toBe(10); // sqrt(100)
  });

  it('clamps the visualMap max to sqrt(1) for an empty grid', () => {
    render(<UsageHeatmap cells={[]} />, { wrapper });
    const visualMap = captured.option!.visualMap as { max: number };
    expect(visualMap.max).toBe(1);
  });

  it('labels the chart and spells out every cell count as text', () => {
    const cells: HeatmapCell[] = [
      { hour: 9, dow: 1, tokens: 100 },
      { hour: 14, dow: 3, tokens: 4 },
    ];
    const { getByTestId, getByText } = render(<UsageHeatmap cells={cells} />, { wrapper });
    expect(getByTestId('echart')).toHaveAttribute('aria-label', 'Activity by hour chart');
    // dow 1 is Monday, dow 3 is Wednesday (SQLite convention: 0 = Sunday).
    expect(getByText('Mon 09:00: 100')).toBeInTheDocument();
    expect(getByText('Wed 14:00: 4')).toBeInTheDocument();
  });

  it('keeps hours across and days down on desktop', () => {
    installMatchMedia(true);
    render(<UsageHeatmap cells={[]} />, { wrapper });
    const opt = captured.option!;
    const xAxis = opt.xAxis as { data: string[]; name: string };
    const yAxis = opt.yAxis as { data: string[] };
    expect(xAxis.data).toEqual(HOURS);
    expect(xAxis.name).toBe('Hour of day');
    expect(yAxis.data).toEqual(DAYS);
    expect(opt.grid).toEqual({ left: 44, right: 12, top: 12, bottom: 84 });
  });

  it('transposes the grid on a phone: days across, hours down', () => {
    installMatchMedia(false);
    const cells: HeatmapCell[] = [
      { hour: 9, dow: 1, tokens: 100 },
      { hour: 14, dow: 3, tokens: 4 },
    ];
    render(<UsageHeatmap cells={cells} />, { wrapper });
    const opt = captured.option!;
    const xAxis = opt.xAxis as { data: string[]; name: string };
    const yAxis = opt.yAxis as { data: string[] };
    expect(xAxis.data).toEqual(DAYS);
    expect(xAxis.name).toBe('Day of week');
    expect(yAxis.data).toEqual(PADDED_HOURS);
    // The tuple follows the axis order: [dow, hour, …] on a phone.
    const series = opt.series as { data: number[][] }[];
    expect(series[0].data).toEqual([
      [1, 9, 10, 100],
      [3, 14, 2, 4],
    ]);
    // The colour still maps on the sqrt-scaled value, not the raw count.
    const visualMap = opt.visualMap as { dimension: number };
    expect(visualMap.dimension).toBe(2);
    // The hour labels moved to the left edge, so the grid gives them less room.
    expect(opt.grid).toEqual({ left: 38, right: 12, top: 12, bottom: 84 });
  });

  it('renders the same tooltip text in both layouts', () => {
    const cells: HeatmapCell[] = [{ hour: 9, dow: 1, tokens: 100 }];

    installMatchMedia(true); // desktop
    const desktop = render(<UsageHeatmap cells={cells} />, { wrapper });
    const desktopText = (captured.option!.tooltip as TooltipOption).formatter({
      value: [9, 1, 10, 100],
    });
    desktop.unmount();

    installMatchMedia(false); // phone
    render(<UsageHeatmap cells={cells} />, { wrapper });
    const phoneText = (captured.option!.tooltip as TooltipOption).formatter({
      value: [1, 9, 10, 100],
    });

    expect(desktopText).toBe('Mon 09:00 — 100 tokens');
    expect(phoneText).toBe(desktopText);
  });

  it('gives the phone layout a taller canvas but respects a caller-provided className', () => {
    installMatchMedia(false);
    render(<UsageHeatmap cells={[]} />, { wrapper });
    expect(captured.className).toBe('h-[420px]');

    installMatchMedia(true);
    render(<UsageHeatmap cells={[]} />, { wrapper });
    expect(captured.className).toBe('h-80');

    installMatchMedia(false);
    render(<UsageHeatmap cells={[]} className="h-52" />, { wrapper });
    expect(captured.className).toBe('h-52');
  });

  it('says what the colour ramp encodes', () => {
    // The scale bar showed numbers that said nothing about what they scaled.
    render(<UsageHeatmap cells={[]} />, { wrapper });
    expect(screen.getByText(/shade shows token volume/i)).toBeInTheDocument();
  });
});
