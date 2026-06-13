import { render, screen } from '@testing-library/react';
import { Gauge } from './Gauge';

function fill(container: HTMLElement): HTMLElement {
  return container.querySelector('[role="meter"] > div') as HTMLElement;
}

describe('Gauge', () => {
  it('renders a meter with the value and aria text', () => {
    render(<Gauge pct={42} status="ok" />);
    const meter = screen.getByRole('meter');
    expect(meter).toHaveAttribute('aria-valuenow', '42');
    expect(meter).toHaveAttribute('aria-valuetext', '42% used');
  });

  it('renders unknown aria text and zero width when pct is null', () => {
    const { container } = render(<Gauge pct={null} status="unknown" />);
    const meter = screen.getByRole('meter');
    expect(meter).toHaveAttribute('aria-valuetext', 'unknown');
    expect(meter).not.toHaveAttribute('aria-valuenow');
    expect(fill(container)).toHaveStyle({ width: '0%' });
  });

  it('treats NaN like null', () => {
    render(<Gauge pct={NaN} status="unknown" />);
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuetext', 'unknown');
  });

  it('clamps values over 100 to a full bar', () => {
    const { container } = render(<Gauge pct={150} status="critical" />);
    expect(fill(container)).toHaveStyle({ width: '100%' });
  });

  it('clamps negative values to zero', () => {
    const { container } = render(<Gauge pct={-20} status="ok" />);
    expect(fill(container)).toHaveStyle({ width: '0%' });
  });

  it('applies the status fill color', () => {
    const { container } = render(<Gauge pct={50} status="warning" />);
    expect(fill(container).className).toContain('bg-warning');
  });

  it('applies the size height class', () => {
    render(<Gauge pct={50} status="ok" size="xl" />);
    expect(screen.getByRole('meter')).toHaveClass('h-3');
  });

  it('renders a glide marker when glide is within (0,100)', () => {
    const { container } = render(<Gauge pct={50} status="ok" glide={70} />);
    const marker = container.querySelector('[title*="glide-path"]') as HTMLElement;
    expect(marker).toBeInTheDocument();
    expect(marker).toHaveStyle({ left: '70%' });
  });

  it('omits the glide marker at boundary / null values', () => {
    const { container: c0 } = render(<Gauge pct={50} status="ok" glide={0} />);
    expect(c0.querySelector('[title*="glide-path"]')).toBeNull();
    const { container: c100 } = render(<Gauge pct={50} status="ok" glide={100} />);
    expect(c100.querySelector('[title*="glide-path"]')).toBeNull();
    const { container: cNull } = render(<Gauge pct={50} status="ok" glide={null} />);
    expect(cNull.querySelector('[title*="glide-path"]')).toBeNull();
  });
});
