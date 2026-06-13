import { render, screen } from '@testing-library/react';
import { Badge } from './Badge';

describe('Badge', () => {
  it('renders its children', () => {
    render(<Badge>Active</Badge>);
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('applies the default neutral variant', () => {
    render(<Badge>Neutral</Badge>);
    expect(screen.getByText('Neutral').className).toContain('text-fg-muted');
  });

  it('applies a named variant', () => {
    render(<Badge variant="critical">Down</Badge>);
    expect(screen.getByText('Down').className).toContain('text-critical');
  });

  it('applies the outline variant border', () => {
    render(<Badge variant="outline">Outlined</Badge>);
    expect(screen.getByText('Outlined').className).toContain('border-edge-strong');
  });

  it('merges a custom className and forwards HTML props', () => {
    render(
      <Badge className="custom-cls" data-testid="badge" title="hi">
        Tag
      </Badge>,
    );
    const el = screen.getByTestId('badge');
    expect(el).toHaveClass('custom-cls');
    expect(el).toHaveAttribute('title', 'hi');
  });
});
