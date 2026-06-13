import { render, screen } from '@testing-library/react';
import { StatusDot } from './StatusDot';

describe('StatusDot', () => {
  it('uses the status as the default aria-label', () => {
    render(<StatusDot status="ok" />);
    expect(screen.getByRole('img', { name: 'ok' })).toBeInTheDocument();
  });

  it('uses a custom label when provided', () => {
    render(<StatusDot status="critical" label="Down" />);
    expect(screen.getByRole('img', { name: 'Down' })).toBeInTheDocument();
  });

  it('applies the status color', () => {
    const { container } = render(<StatusDot status="warning" />);
    expect(container.querySelector('.bg-warning')).toBeInTheDocument();
  });

  it('renders a pulse ring only when pulse is set', () => {
    const { container: withPulse } = render(<StatusDot status="critical" pulse />);
    expect(withPulse.querySelector('.animate-ping')).toBeInTheDocument();
    const { container: noPulse } = render(<StatusDot status="ok" />);
    expect(noPulse.querySelector('.animate-ping')).toBeNull();
  });
});
