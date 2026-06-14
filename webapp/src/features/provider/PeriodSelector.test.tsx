import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setTzConfig } from '@/lib/tz';
import { PeriodSelector } from './PeriodSelector';
import { currentMonthKey, shiftMonthKey } from './period';

beforeEach(() => setTzConfig({ user_timezone: 'UTC', env_timezone: null }));
afterEach(() => vi.useRealTimers());

// Pin "now" so current-month assertions don't depend on the wall clock.
function freezeJune2026() {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-06-14T12:00:00Z'));
  setTzConfig({ user_timezone: 'UTC', env_timezone: null });
}

describe('PeriodSelector', () => {
  it('renders the month label and disables next on the current month', () => {
    freezeJune2026();
    render(<PeriodSelector value="2026-06" onChange={() => {}} earliest={null} />);
    expect(screen.getByText('June 2026')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /next month/i })).toBeDisabled();
    // No "Today" shortcut when already on the current month.
    expect(screen.queryByRole('button', { name: /today/i })).not.toBeInTheDocument();
  });

  it('disables previous at the earliest recorded month', () => {
    freezeJune2026();
    render(<PeriodSelector value="2026-03" onChange={() => {}} earliest="2026-03-10T00:00:00Z" />);
    expect(screen.getByRole('button', { name: /previous month/i })).toBeDisabled();
  });

  it('emits the prev/next/today keys', async () => {
    // Real timers here (userEvent relies on them); expectations are derived
    // from the actual current month so the test is clock-independent.
    const onChange = vi.fn();
    render(<PeriodSelector value="2026-03" onChange={onChange} earliest={null} />);

    await userEvent.click(screen.getByRole('button', { name: /previous month/i }));
    expect(onChange).toHaveBeenLastCalledWith(shiftMonthKey('2026-03', -1));

    await userEvent.click(screen.getByRole('button', { name: /next month/i }));
    expect(onChange).toHaveBeenLastCalledWith(shiftMonthKey('2026-03', 1));

    await userEvent.click(screen.getByRole('button', { name: /today/i }));
    expect(onChange).toHaveBeenLastCalledWith(currentMonthKey());
  });
});
