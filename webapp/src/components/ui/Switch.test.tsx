import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Switch } from './Switch';

describe('Switch', () => {
  it('renders a switch role', () => {
    render(<Switch />);
    expect(screen.getByRole('switch')).toBeInTheDocument();
  });

  it('reflects the checked state', () => {
    render(<Switch checked onCheckedChange={() => {}} />);
    expect(screen.getByRole('switch')).toBeChecked();
  });

  it('fires onCheckedChange on click', async () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onCheckedChange={onChange} />);
    await userEvent.click(screen.getByRole('switch'));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('is disabled when the disabled prop is set', async () => {
    const onChange = vi.fn();
    render(<Switch disabled onCheckedChange={onChange} />);
    const sw = screen.getByRole('switch');
    expect(sw).toBeDisabled();
    await userEvent.click(sw);
    expect(onChange).not.toHaveBeenCalled();
  });
});
