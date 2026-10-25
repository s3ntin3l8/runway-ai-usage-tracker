import { render } from '@testing-library/react';
import { TokenBar } from './TokenBar';

// Bar segments carry a title="<label>: <value>"; legend dots do not — so
// querying [title] isolates the proportional segments.
function segments(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>('[title]'));
}

function colorOf(container: HTMLElement, label: string): string | undefined {
  return segments(container).find((s) => s.getAttribute('title')?.startsWith(`${label}:`))
    ?.style.background;
}

describe('TokenBar', () => {
  it('sizes segments proportionally to their values', () => {
    const { container } = render(
      <TokenBar tokens={{ tokens_input: 100, tokens_output: 300 }} />,
    );
    const segs = segments(container);
    expect(segs).toHaveLength(2);
    expect(segs[0].style.width).toBe('25%'); // input 100 / 400
    expect(segs[1].style.width).toBe('75%'); // output 300 / 400
  });

  it('drops cache segments and renormalizes when excludeCache is set', () => {
    const { container } = render(
      <TokenBar
        tokens={{ tokens_input: 100, tokens_cache_read: 200, tokens_cache_create: 100 }}
        excludeCache
      />,
    );
    const segs = segments(container);
    expect(segs).toHaveLength(1); // only input survives
    expect(segs[0].style.width).toBe('100%');
    expect(segs[0].getAttribute('title')).toContain('Input');
  });

  it('keeps cache segments when excludeCache is off', () => {
    const { container } = render(
      <TokenBar tokens={{ tokens_input: 100, tokens_cache_read: 100 }} />,
    );
    expect(segments(container)).toHaveLength(2);
  });

  it('renders nothing when the total is zero', () => {
    const { container } = render(<TokenBar tokens={{ tokens_input: 0 }} />);
    expect(container.firstChild).toBeNull();
  });

  it('lists only non-zero entries in the legend', () => {
    const { getByText, queryByText } = render(
      <TokenBar tokens={{ tokens_input: 1000, tokens_output: 0 }} showLegend />,
    );
    expect(getByText(/Input/)).toBeInTheDocument();
    expect(queryByText(/Output/)).toBeNull();
  });

  it('colours a segment by its key, not by its position in the bar', () => {
    const tokens = {
      tokens_input: 100,
      tokens_output: 200,
      tokens_cache_read: 300,
      tokens_cache_create: 400,
      tokens_reasoning: 500,
    };
    const { container: withCache } = render(<TokenBar tokens={tokens} />);
    const { container: withoutCache } = render(<TokenBar tokens={tokens} excludeCache />);

    // The slot belongs to the segment, so excluding cache must not renumber
    // the colours of the segments that remain.
    expect(colorOf(withCache, 'Reasoning')).toBe('var(--chart-3)');
    expect(colorOf(withoutCache, 'Reasoning')).toBe('var(--chart-3)');
    expect(colorOf(withCache, 'Cache read')).toBe('var(--chart-4)');
    expect(colorOf(withCache, 'Input')).toBe('var(--chart-1)');
  });

  it('names the bar for assistive tech', () => {
    const { getByRole } = render(
      <TokenBar tokens={{ tokens_input: 1000, tokens_output: 2000 }} />,
    );
    expect(getByRole('img')).toHaveAttribute(
      'aria-label',
      'Token composition: Input 1K, Output 2K',
    );
  });
});
