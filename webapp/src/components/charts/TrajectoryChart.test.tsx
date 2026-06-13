import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { ForecastEntry } from '@/api/types';

const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" />;
  },
}));

import { TrajectoryChart } from './TrajectoryChart';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

type Series = { name: string; data: number[][]; lineStyle: { color: string } }[];

const base: ForecastEntry = {
  provider_id: 'claude',
  account_id: 'default',
  status: 'ok',
};

describe('TrajectoryChart', () => {
  it('builds the observed series from the series points', () => {
    const forecast: ForecastEntry = {
      ...base,
      series: [
        { ts: '2026-06-13T00:00:00Z', pct: 10 },
        { ts: '2026-06-13T01:00:00Z', pct: 20 },
      ],
    };
    render(<TrajectoryChart forecast={forecast} />, { wrapper });
    const series = captured.option!.series as Series;
    expect(series[0].name).toBe('Used');
    expect(series[0].data).toEqual([
      [Date.parse('2026-06-13T00:00:00Z'), 10],
      [Date.parse('2026-06-13T01:00:00Z'), 20],
    ]);
  });

  it('adds a projection segment when reset_at and projected_pct are present', () => {
    const forecast: ForecastEntry = {
      ...base,
      series: [{ ts: '2026-06-13T00:00:00Z', pct: 20 }],
      reset_at: '2026-06-14T00:00:00Z',
      projected_pct: 80,
    };
    render(<TrajectoryChart forecast={forecast} />, { wrapper });
    const projection = (captured.option!.series as Series)[1];
    expect(projection.name).toBe('Projected');
    expect(projection.data).toEqual([
      [Date.parse('2026-06-13T00:00:00Z'), 20],
      [Date.parse('2026-06-14T00:00:00Z'), 80],
    ]);
  });

  it('leaves the projection empty without a reset boundary', () => {
    const forecast: ForecastEntry = {
      ...base,
      series: [{ ts: '2026-06-13T00:00:00Z', pct: 20 }],
    };
    render(<TrajectoryChart forecast={forecast} />, { wrapper });
    expect((captured.option!.series as Series)[1].data).toEqual([]);
  });

  it('handles a missing series array without throwing', () => {
    render(<TrajectoryChart forecast={{ ...base }} />, { wrapper });
    expect((captured.option!.series as Series)[0].data).toEqual([]);
  });
});
