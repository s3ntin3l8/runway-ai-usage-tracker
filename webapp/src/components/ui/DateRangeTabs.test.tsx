import { screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { DateRangeTabs, type DateRangeValue } from './DateRangeTabs';

let value: DateRangeValue = { days: 7 };
let onChange = vi.fn((v: DateRangeValue) => { value = v; });

function setup(initial?: Partial<DateRangeValue>) {
  value = { days: 7, ...initial };
  onChange = vi.fn((v: DateRangeValue) => { value = v; });
}

describe('DateRangeTabs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setup();
  });

  it('renders preset tabs', () => {
    renderWithProviders(<DateRangeTabs value={value} onChange={onChange} />);
    expect(screen.getByRole('tab', { name: '7d' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '14d' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '30d' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '90d' })).toBeInTheDocument();
  });

  it('calls onChange with the selected days', async () => {
    renderWithProviders(<DateRangeTabs value={value} onChange={onChange} />);
    await userEvent.click(screen.getByRole('tab', { name: '30d' }));
    expect(onChange).toHaveBeenCalledWith({ days: 30 });
  });

  it('opens the calendar popover when the calendar button is clicked', async () => {
    renderWithProviders(<DateRangeTabs value={value} onChange={onChange} />);
    const btn = screen.getByRole('button');
    await userEvent.click(btn);
    expect(await screen.findByText('Custom range')).toBeInTheDocument();
  });

  it('applies a custom range when dates are selected and Apply is clicked', async () => {
    renderWithProviders(<DateRangeTabs value={value} onChange={onChange} />);
    const btn = screen.getByRole('button');
    await userEvent.click(btn);
    await screen.findByText('Custom range');
    const dateInputs = document.querySelectorAll('input[type="date"]');
    const fromInput = dateInputs[0] as HTMLInputElement;
    const toInput = dateInputs[1] as HTMLInputElement;
    fireEvent.change(fromInput, { target: { value: '2026-01-01' } });
    fireEvent.change(toInput, { target: { value: '2026-01-15' } });
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(onChange).toHaveBeenCalledWith({ since: '2026-01-01', until: '2026-01-15' });
  });

  it('swaps reversed dates automatically', async () => {
    renderWithProviders(<DateRangeTabs value={value} onChange={onChange} />);
    const btn = screen.getByRole('button');
    await userEvent.click(btn);
    await screen.findByText('Custom range');
    const dateInputs = document.querySelectorAll('input[type="date"]');
    const fromInput = dateInputs[0] as HTMLInputElement;
    const toInput = dateInputs[1] as HTMLInputElement;
    fireEvent.change(fromInput, { target: { value: '2026-01-20' } });
    fireEvent.change(toInput, { target: { value: '2026-01-01' } });
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(onChange).toHaveBeenCalledWith({ since: '2026-01-01', until: '2026-01-20' });
  });
});
