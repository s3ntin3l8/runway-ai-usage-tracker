import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';

const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" />;
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

  it('sort order places largest bar first', () => {
    render(<RankBar rows={mockRows} format={format} />, { wrapper });
    const yAxis = captured.option!.yAxis as { data: string[] };
    expect(yAxis.data[0]).toBe('project-b');
    expect(yAxis.data[1]).toBe('project-a');
  });

  it('label truncation at 10 chars', () => {
    const rows: RankRow[] = [
      { label: 'a-very-long-name', value: 10 },
    ];
    render(<RankBar rows={rows} format={format} />, { wrapper });
    const yAxis = captured.option!.yAxis as {
      axisLabel: { formatter: (v: string) => string };
    };
    expect(yAxis.axisLabel.formatter('a-very-long-name')).toBe('a-very-lon…');
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
});
