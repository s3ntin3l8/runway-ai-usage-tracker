import { render, screen } from '@testing-library/react';
import { StatTile } from './StatTile';

describe('StatTile', () => {
  it('renders label and value', () => {
    render(<StatTile label="Tokens" value="1.2M" />);
    expect(screen.getByText('Tokens')).toBeInTheDocument();
    expect(screen.getByText('1.2M')).toBeInTheDocument();
  });

  it('renders a hint when provided', () => {
    render(<StatTile label="Cost" value="$5" hint="this month" />);
    expect(screen.getByText('this month')).toBeInTheDocument();
  });

  it('shows a skeleton instead of the value when loading', () => {
    const { container } = render(<StatTile label="Cost" value="$5" loading />);
    expect(screen.queryByText('$5')).not.toBeInTheDocument();
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });

  it('tints the value by status', () => {
    render(<StatTile label="Usage" value="95%" status="critical" />);
    expect(screen.getByText('95%')).toHaveClass('text-critical');
  });

  it('leaves the value untinted for ok/no status', () => {
    render(<StatTile label="Usage" value="10%" />);
    expect(screen.getByText('10%').className).not.toContain('text-critical');
  });
});
