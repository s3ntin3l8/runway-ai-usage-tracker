import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import { formatCost } from '@/lib/format';

const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  // The mock forwards ariaLabel the way the real EChart does, so the tests
  // can assert that each chart hands its label down.
  EChart: ({
    option,
    ariaLabel,
  }: {
    option: Record<string, unknown>;
    ariaLabel?: string;
  }) => {
    captured.option = option;
    return (
      <div
        data-testid="echart"
        role={ariaLabel ? 'img' : undefined}
        aria-label={ariaLabel}
      />
    );
  },
}));

import { RankBar } from './RankBar';
import type { RankRow } from './RankBar';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

const format = (v: number) => v.toLocaleString();

const mockRows: RankRow[] = [
  { label: 'project-a', value: 100, sub: 'via anthropic', segments: [
    { key: 'tokens_input', label: 'Input', color: '#f00', value: 60 },
    { key: 'tokens_output', label: 'Output', color: '#0f0', value: 40 },
  ]},
  { label: 'project-b', value: 50, segments: [
    { key: 'tokens_input', label: 'Input', color: '#f00', value: 30 },
    { key: 'tokens_output', label: 'Output', color: '#0f0', value: 20 },
  ]},
];

describe('RankBar', () => {
  beforeEach(() => {
    captured.option = undefined;
  });

  it('renders stacked bars when segments present', () => {
    render(<RankBar rows={mockRows} format={format} />, { wrapper });
    const series = captured.option!.series as { name: string; stack: string; data: number[] }[];
    expect(series).toHaveLength(2);
    expect(series[0].name).toBe('Input');
    expect(series[0].stack).toBe('total');
    expect(series[1].name).toBe('Output');
    expect(series[1].stack).toBe('total');
  });

  it('uses axisFormat for x-axis ticks and hides overlapping labels', () => {
    render(<RankBar rows={mockRows} format={format} axisFormat={(v) => `~${v}`} />, { wrapper });
    const axisLabel = (
      captured.option!.xAxis as {
        axisLabel: { formatter: (v: number) => string; hideOverlap: boolean };
      }
    ).axisLabel;
    expect(axisLabel.formatter(1500)).toBe('~1500');
    expect(axisLabel.hideOverlap).toBe(true);
  });

  it('falls back to format for ticks without axisFormat', () => {
    render(<RankBar rows={mockRows} format={format} />, { wrapper });
    const axisLabel = (
      captured.option!.xAxis as { axisLabel: { formatter: (v: number) => string } }
    ).axisLabel;
    expect(axisLabel.formatter(1500)).toBe(format(1500));
  });

  it('renders single bar when no segments', () => {
    const rows: RankRow[] = [
      { label: 'alpha', value: 200 },
      { label: 'beta', value: 80 },
    ];
    render(<RankBar rows={rows} format={format} />, { wrapper });
    const series = captured.option!.series as { type: string; data: number[] }[];
    expect(series).toHaveLength(1);
    expect(series[0].type).toBe('bar');
    expect(series[0].data).toEqual([80, 200]);
  });

  it('filters out zero-value rows', () => {
    const rows: RankRow[] = [
      { label: 'keep', value: 50 },
      { label: 'drop', value: 0 },
    ];
    render(<RankBar rows={rows} format={format} />, { wrapper });
    const yAxis = captured.option!.yAxis as { data: string[] };
    expect(yAxis.data).toEqual(['keep']);
  });

  it('keeps a row whose drawn segments are zero but whose reported total is not', () => {
    // A provider that reports its own cost has no rate configured, so the
    // segments sum to zero while the total the backend reports is not.
    // Filtering on the drawn sum alone would drop the one row that has a
    // number to disclose.
    const rows: RankRow[] = [
      { label: 'keep', value: 50 },
      { label: 'reported-only', value: 0, reportedValue: 120 },
    ];
    render(<RankBar rows={rows} format={format} />, { wrapper });
    const yAxis = captured.option!.yAxis as { data: string[] };
    // Sorted by the drawn value, so the row with no drawn bar sits first.
    expect(yAxis.data).toEqual(['reported-only', 'keep']);
  });

  it('sort order places largest bar first', () => {
    render(<RankBar rows={mockRows} format={format} />, { wrapper });
    const yAxis = captured.option!.yAxis as { data: string[] };
    expect(yAxis.data[0]).toBe('project-b');
    expect(yAxis.data[1]).toBe('project-a');
  });

  it('lets ECharts truncate the axis label instead of slicing it in JS', () => {
    const rows: RankRow[] = [
      { label: 'a-very-long-name', value: 10 },
    ];
    render(<RankBar rows={rows} format={format} />, { wrapper });
    const yAxis = captured.option!.yAxis as {
      data: string[];
      axisLabel: {
        width: number;
        overflow: string;
        ellipsis: string;
        formatter?: (v: string) => string;
      };
    };
    // ECharts measures the label and cuts it to the pixel budget itself, so
    // the label adapts to the chart instead of being cut at a character count.
    expect(yAxis.axisLabel.overflow).toBe('truncate');
    expect(yAxis.axisLabel.ellipsis).toBe('…');
    expect(yAxis.axisLabel.width).toBeGreaterThan(0);
    // ...and there is no JS slice left that would cut the name mid-word.
    expect(yAxis.axisLabel.formatter).toBeUndefined();
    // The label itself reaches the chart (and the tooltip) untruncated.
    expect(yAxis.data).toEqual(['a-very-long-name']);
  });

  it('discloses the backend total next to the drawn total when they differ', () => {
    const rows: RankRow[] = [
      {
        label: 'project-a',
        value: 100,
        reportedValue: 120,
        segments: [{ key: 'tokens_input', label: 'Input', color: '#f00', value: 100 }],
      },
    ];
    render(<RankBar rows={rows} format={format} />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([{ seriesName: 'Input', value: 100, dataIndex: 0 }]);
    // Both numbers are on screen: the drawn segments' sum AND the total the
    // backend reports.
    expect(result).toContain('Total: 100');
    expect(result).toContain('Reported: 120');
  });

  it('prints only the total when the drawn sum matches the reported total', () => {
    const rows: RankRow[] = [
      {
        label: 'project-a',
        value: 100,
        reportedValue: 100,
        segments: [{ key: 'tokens_input', label: 'Input', color: '#f00', value: 100 }],
      },
    ];
    render(<RankBar rows={rows} format={format} />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([{ seriesName: 'Input', value: 100, dataIndex: 0 }]);
    expect(result).toContain('Total: 100');
    expect(result).not.toContain('Reported');
  });

  it('treats float noise as agreement, not as a disagreement', () => {
    const rows: RankRow[] = [
      {
        label: 'project-a',
        value: 100,
        // 0.1 + 0.2 = 0.30000000000000004: the same number, in binary.
        reportedValue: 100 + 1e-13,
        segments: [{ key: 'tokens_input', label: 'Input', color: '#f00', value: 100 }],
      },
    ];
    // A formatter that WOULD show the difference, so only the numeric
    // threshold can hide it.
    render(<RankBar rows={rows} format={(v) => v.toString()} />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([{ seriesName: 'Input', value: 100, dataIndex: 0 }]);
    expect(result).not.toContain('Reported');
  });

  it('hides a difference the formatter renders identically', () => {
    const rows: RankRow[] = [
      {
        label: 'project-a',
        value: 0.15,
        // Both render as "$0.15": a fraction of a cent is not a difference
        // the reader can see, so printing both numbers would only look like
        // a bug.
        reportedValue: 0.1504,
        segments: [{ key: 'cost_input', label: 'Input', color: '#f00', value: 0.15 }],
      },
    ];
    render(<RankBar rows={rows} format={(v) => formatCost(v)} />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([{ seriesName: 'Input', value: 0.15, dataIndex: 0 }]);
    expect(result).toContain('Total: $0.15');
    expect(result).not.toContain('Reported');
  });

  it('carries the disclosed backend total into the text alternative', () => {
    const rows: RankRow[] = [
      {
        label: 'project-a',
        value: 100,
        reportedValue: 120,
        sub: 'via anthropic',
        segments: [{ key: 'tokens_input', label: 'Input', color: '#f00', value: 100 }],
      },
    ];
    const { getByText } = render(<RankBar rows={rows} format={format} />, { wrapper });
    expect(getByText(/project-a: 100 \(via anthropic · reported 120\)/)).toBeInTheDocument();
  });

  it('tooltip includes sub line when present', () => {
    render(<RankBar rows={mockRows} format={format} />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([
      { seriesName: 'Input', value: 60, dataIndex: 1 },
      { seriesName: 'Output', value: 40, dataIndex: 1 },
    ]);
    expect(result).toContain('via anthropic');
  });

  it('empty rows produces empty config', () => {
    render(<RankBar rows={[]} format={format} />, { wrapper });
    const series = captured.option!.series as { data: number[] }[];
    expect(series).toHaveLength(1);
    expect(series[0].data).toEqual([]);
    const yAxis = captured.option!.yAxis as { data: string[] };
    expect(yAxis.data).toEqual([]);
  });

  it('gives the stacked bars a legend naming the segments', () => {
    document.documentElement.style.setProperty('--fg-muted', '#9a9aa2');
    render(<RankBar rows={mockRows} format={format} />, { wrapper });
    const legend = captured.option!.legend as {
      bottom: number;
      icon: string;
      itemWidth: number;
      textStyle: { color: string };
    };
    expect(legend.bottom).toBe(0);
    expect(legend.icon).toBe('square');
    // The legend text follows the theme token, so it reads on both themes.
    expect(legend.textStyle.color).toBe('#9a9aa2');
    document.documentElement.style.removeProperty('--fg-muted');
  });

  it('leaves room for the legend under the stacked bars', () => {
    render(<RankBar rows={mockRows} format={format} />, { wrapper });
    const grid = captured.option!.grid as { bottom: number };
    // Two wrapped legend lines (≈48px) must not cover the x-axis labels.
    expect(grid.bottom).toBeGreaterThanOrEqual(48);
  });

  it('has no legend for the single-value bars', () => {
    render(<RankBar rows={[{ label: 'alpha', value: 200 }]} format={format} />, { wrapper });
    expect(captured.option!.legend).toBeUndefined();
  });

  it('labels the chart and exposes the row and segment values as text', () => {
    const { getByTestId, getByText } = render(
      <RankBar rows={mockRows} format={format} />,
      { wrapper },
    );
    expect(getByTestId('echart')).toHaveAttribute('aria-label', 'Ranking chart');
    // Rows are listed in the order the chart draws them (largest last); the
    // row's optional subtitle rides along in parentheses.
    expect(getByText(/project-b: 50/)).toBeInTheDocument();
    expect(getByText(/project-a: 100 \(via anthropic\)/)).toBeInTheDocument();
    // ...with their segment breakdown.
    expect(getByText('Input: 30')).toBeInTheDocument();
    expect(getByText('Output: 40')).toBeInTheDocument();
  });
});
