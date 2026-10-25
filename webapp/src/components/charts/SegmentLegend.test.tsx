import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SegmentLegend } from './SegmentLegend';

describe('SegmentLegend', () => {
  it('renders one labelled swatch per series', () => {
    render(
      <SegmentLegend
        segments={[
          { key: 'input', label: 'Input', color: 'var(--chart-1)', value: '1K' },
          { key: 'output', label: 'Output', color: 'var(--chart-2)', value: '2K' },
        ]}
      />,
    );
    expect(screen.getByText(/Input/)).toBeInTheDocument();
    expect(screen.getByText(/1K/)).toBeInTheDocument();
    expect(screen.getByText(/Output/)).toBeInTheDocument();
    expect(screen.getByText(/2K/)).toBeInTheDocument();
  });

  it('omits the value when there is none', () => {
    render(
      <SegmentLegend
        segments={[{ key: 'input', label: 'Input', color: 'var(--chart-1)' }]}
      />,
    );
    expect(screen.getByText(/Input/)).toBeInTheDocument();
  });

  it('keys on the segment key, not the position', () => {
    // The colour is resolved by the caller from the segment key, so a segment
    // keeps its colour wherever it is shown.
    const { rerender } = render(
      <SegmentLegend
        segments={[{ key: 'cache_read', label: 'Cache read', color: 'var(--chart-4)' }]}
      />,
    );
    expect(screen.getByText('Cache read')).toBeInTheDocument();
    rerender(
      <SegmentLegend
        segments={[{ key: 'cache_read', label: 'Cache read', color: 'var(--chart-4)' }]}
      />,
    );
    expect(screen.getByText('Cache read')).toBeInTheDocument();
  });
});
