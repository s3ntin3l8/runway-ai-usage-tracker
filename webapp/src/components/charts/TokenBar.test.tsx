import { render } from '@testing-library/react';
import { TokenBar } from './TokenBar';

// Bar segments carry a title="<label>: <value>"; legend dots do not — so
// querying [title] isolates the proportional segments.
function segments(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>('[title]'));
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
});
