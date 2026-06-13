import { render, screen, act } from '@testing-library/react';
import { Countdown } from './Countdown';

describe('Countdown', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T12:00:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing when until is null', () => {
    const { container } = render(<Countdown until={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing for an invalid timestamp', () => {
    const { container } = render(<Countdown until="not-a-date" />);
    expect(container.firstChild).toBeNull();
  });

  it('renders the default prefix and remaining time', () => {
    // 4h 30m in the future
    render(<Countdown until="2026-06-13T16:30:00Z" />);
    expect(screen.getByText(/resets in/)).toHaveTextContent('resets in 4h 30m');
  });

  it('supports a custom prefix', () => {
    render(<Countdown until="2026-06-13T13:00:00Z" prefix="expires in" />);
    expect(screen.getByText(/expires in/)).toHaveTextContent('expires in 1h');
  });

  it('re-renders on the minute tick', () => {
    // Reset 10m out; one minute of real elapsed time should drop it to 9m.
    render(<Countdown until="2026-06-13T12:10:30Z" />);
    expect(screen.getByText(/resets in/)).toHaveTextContent('10m');
    // advanceTimersByTime moves the (faked) clock forward AND fires the interval.
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText(/resets in/)).toHaveTextContent('9m');
  });

  it('applies a custom className', () => {
    render(<Countdown until="2026-06-13T13:00:00Z" className="my-cls" />);
    expect(screen.getByText(/resets in/)).toHaveClass('my-cls');
  });
});
