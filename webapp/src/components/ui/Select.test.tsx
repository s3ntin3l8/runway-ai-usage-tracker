import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from './Select';

function Harness({ onValueChange }: { onValueChange?: (v: string) => void }) {
  return (
    <Select defaultValue="day" onValueChange={onValueChange}>
      <SelectTrigger aria-label="Window">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="day">Day</SelectItem>
        <SelectItem value="week">Week</SelectItem>
      </SelectContent>
    </Select>
  );
}

describe('Select', () => {
  it('renders the trigger with the current value', () => {
    render(<Harness />);
    const trigger = screen.getByRole('combobox', { name: 'Window' });
    expect(trigger).toBeInTheDocument();
    expect(trigger).toHaveTextContent('Day');
  });

  it('opens and selects an option, firing onValueChange', async () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('combobox', { name: 'Window' }));
    // Radix renders the listbox options in a portal once open.
    const week = await screen.findByRole('option', { name: 'Week' });
    await user.click(week);
    expect(onValueChange).toHaveBeenCalledWith('week');
  });
});
