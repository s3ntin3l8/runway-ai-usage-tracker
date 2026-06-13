import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { CumulativeModelBucket } from '@/api/types';

const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" />;
  },
}));

import { TokenDonut } from './TokenDonut';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

type Series = { data: { name: string; value: number }[] }[];

describe('TokenDonut', () => {
  it('renders one slice per non-zero token category', () => {
    const bucket: CumulativeModelBucket = {
      tokens_input: 100,
      tokens_output: 40,
      tokens_cache_read: 0,
      tokens_reasoning: 10,
    };
    render(<TokenDonut bucket={bucket} />, { wrapper });
    const data = (captured.option!.series as Series)[0].data;
    expect(data).toEqual([
      { name: 'Input', value: 100 },
      { name: 'Output', value: 40 },
      { name: 'Reasoning', value: 10 },
    ]);
  });

  it('drops every slice for an empty bucket', () => {
    render(<TokenDonut bucket={{}} />, { wrapper });
    expect((captured.option!.series as Series)[0].data).toEqual([]);
  });
});
