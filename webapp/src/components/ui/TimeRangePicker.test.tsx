import { screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { TimeRangePicker } from './TimeRangePicker';
import { todayISODate, type DateRangeValue } from '@/lib/timeRange';

let value: DateRangeValue = { days: 7 };
let onChange = vi.fn((v: DateRangeValue) => {
  value = v;
});

function setup(initial?: Partial<DateRangeValue>) {
  value = { days: 7, ...initial };
  onChange = vi.fn((v: DateRangeValue) => {
    value = v;
  });
}

describe('TimeRangePicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setup();
  });

  it('shows the current range label on the trigger', () => {
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    expect(screen.getByRole('button', { name: 'Last 7 days' })).toBeInTheDocument();
  });

  it('labels absolute and month ranges on the trigger', () => {
    setup({ since: '2026-06-01', until: '2026-06-30' });
    const { unmount } = renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    expect(screen.getByRole('button', { name: 'June 2026' })).toBeInTheDocument();
    unmount();

    setup({ since: '2026-01-05', until: '2026-01-20' });
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    expect(screen.getByRole('button', { name: 'Jan 5 – Jan 20' })).toBeInTheDocument();
  });

  it('opens a popover with quick ranges and absolute inputs', async () => {
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    expect(await screen.findByText('Quick ranges')).toBeInTheDocument();
    expect(screen.getByText('Absolute time range')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Last 30 days' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'This month' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Last month' })).toBeInTheDocument();
    expect(document.querySelectorAll('input[type="date"]')).toHaveLength(2);
  });

  it('marks the active quick range and applies a new one', async () => {
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    expect(await screen.findByText('Quick ranges')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Last 7 days', pressed: true })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));
    expect(onChange).toHaveBeenCalledWith({ days: 30 });
  });

  it('applies an absolute range when dates are selected and Apply is clicked', async () => {
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await screen.findByText('Absolute time range');
    const [fromInput, toInput] = document.querySelectorAll('input[type="date"]');
    fireEvent.change(fromInput, { target: { value: '2026-01-01' } });
    fireEvent.change(toInput, { target: { value: '2026-01-15' } });
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(onChange).toHaveBeenCalledWith({ since: '2026-01-01', until: '2026-01-15' });
  });

  it('swaps reversed dates automatically', async () => {
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await screen.findByText('Absolute time range');
    const [fromInput, toInput] = document.querySelectorAll('input[type="date"]');
    fireEvent.change(fromInput, { target: { value: '2026-01-20' } });
    fireEvent.change(toInput, { target: { value: '2026-01-01' } });
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(onChange).toHaveBeenCalledWith({ since: '2026-01-01', until: '2026-01-20' });
  });

  it('caps the From input at the earliest available instant', async () => {
    renderWithProviders(
      <TimeRangePicker value={value} onChange={onChange} earliest="2026-03-15T00:00:00Z" />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await screen.findByText('Absolute time range');
    const [fromInput] = document.querySelectorAll('input[type="date"]');
    expect(fromInput).toHaveAttribute('min', '2026-03-15');
  });

  it('caps both date inputs at today', async () => {
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await screen.findByText('Absolute time range');
    const [fromInput, toInput] = document.querySelectorAll('input[type="date"]');
    expect(fromInput).toHaveAttribute('max', todayISODate());
    expect(toInput).toHaveAttribute('max', todayISODate());
  });

  it('clamps a future end date to today on Apply', async () => {
    renderWithProviders(<TimeRangePicker value={value} onChange={onChange} />);
    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await screen.findByText('Absolute time range');
    const [fromInput, toInput] = document.querySelectorAll('input[type="date"]');
    fireEvent.change(fromInput, { target: { value: '2026-01-01' } });
    fireEvent.change(toInput, { target: { value: '2099-01-15' } });
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(onChange).toHaveBeenCalledWith({ since: '2026-01-01', until: todayISODate() });
  });
});
