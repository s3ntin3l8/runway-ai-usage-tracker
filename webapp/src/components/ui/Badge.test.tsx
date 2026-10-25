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

  it('bounds its own width so a long label cannot widen its container', () => {
    render(<Badge data-testid="badge">claude-sonnet-4-5-20250929</Badge>);
    const el = screen.getByTestId('badge');
    // A badge is a flex child: without min-w-0 it refuses to shrink below
    // its content, and without max-w-full it can exceed its cell. truncate
    // then clips the overflow instead of pushing the layout wider.
    expect(el).toHaveClass('max-w-full');
    expect(el).toHaveClass('min-w-0');
    expect(el).toHaveClass('truncate');
  });
});
