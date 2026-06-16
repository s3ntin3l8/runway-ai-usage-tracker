import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setTzConfig } from '@/lib/tz';
import { ScopeSelector } from './ScopeSelector';
import { currentMonthKey, shiftMonthKey } from './period';

beforeEach(() => setTzConfig({ user_timezone: 'UTC', env_timezone: null }));
afterEach(() => vi.useRealTimers());

// Pin "now" so current-month assertions don't depend on the wall clock.
function freezeJune2026() {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-06-14T12:00:00Z'));
  setTzConfig({ user_timezone: 'UTC', env_timezone: null });
}

describe('ScopeSelector — month mode', () => {
  it('renders the month label and disables next on the current month', () => {
    freezeJune2026();
    render(<ScopeSelector value="2026-06" mode="month" onChange={() => {}} earliest={null} />);
    expect(screen.getByText('June 2026')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /next month/i })).toBeDisabled();
    // No "Today" shortcut when already on the current month.
    expect(screen.queryByRole('button', { name: /today/i })).not.toBeInTheDocument();
  });

  it('disables previous at the earliest recorded month', () => {
    freezeJune2026();
    render(
      <ScopeSelector
        value="2026-03"
        mode="month"
        onChange={() => {}}
        earliest="2026-03-10T00:00:00Z"
      />,
    );
    expect(screen.getByRole('button', { name: /previous month/i })).toBeDisabled();
  });

  it('emits the prev/next/today keys', async () => {
    // Real timers here (userEvent relies on them); expectations are derived
    // from the actual current month so the test is clock-independent.
    const onChange = vi.fn();
    render(<ScopeSelector value="2026-03" mode="month" onChange={onChange} earliest={null} />);

    await userEvent.click(screen.getByRole('button', { name: /previous month/i }));
    expect(onChange).toHaveBeenLastCalledWith(shiftMonthKey('2026-03', -1));

    await userEvent.click(screen.getByRole('button', { name: /next month/i }));
    expect(onChange).toHaveBeenLastCalledWith(shiftMonthKey('2026-03', 1));

    await userEvent.click(screen.getByRole('button', { name: /today/i }));
    expect(onChange).toHaveBeenLastCalledWith(currentMonthKey());
  });

  it('switches to the default rolling window when Rolling is selected', async () => {
    const onChange = vi.fn();
    render(<ScopeSelector value="2026-06" mode="month" onChange={onChange} earliest={null} />);
    await userEvent.click(screen.getByRole('tab', { name: 'Rolling' }));
    expect(onChange).toHaveBeenLastCalledWith('30d');
  });
});

describe('ScopeSelector — rolling mode', () => {
  it('renders the day-range tabs and emits the picked window', async () => {
    const onChange = vi.fn();
    render(<ScopeSelector value="30d" mode="rolling" onChange={onChange} earliest={null} />);
    // The month stepper is replaced by the rolling controls.
    expect(screen.queryByRole('button', { name: /previous month/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: '7d' }));
    expect(onChange).toHaveBeenLastCalledWith('7d');
  });

  it('returns to the current month when Month is selected', async () => {
    const onChange = vi.fn();
    render(<ScopeSelector value="30d" mode="rolling" onChange={onChange} earliest={null} />);
    await userEvent.click(screen.getByRole('tab', { name: 'Month' }));
    expect(onChange).toHaveBeenLastCalledWith(currentMonthKey());
  });
});
