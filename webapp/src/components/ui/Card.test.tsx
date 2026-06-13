import { render, screen } from '@testing-library/react';
import { Card, CardHeader, CardTitle, CardContent } from './Card';

describe('Card', () => {
  it('renders the full composition', () => {
    render(
      <Card data-testid="card">
        <CardHeader data-testid="header">
          <CardTitle>My Title</CardTitle>
        </CardHeader>
        <CardContent data-testid="content">Body</CardContent>
      </Card>,
    );
    expect(screen.getByTestId('card')).toHaveClass('bg-surface-1');
    expect(screen.getByTestId('header')).toHaveClass('flex');
    expect(screen.getByRole('heading', { name: 'My Title' })).toBeInTheDocument();
    expect(screen.getByTestId('content')).toHaveTextContent('Body');
  });

  it('CardTitle renders an h2', () => {
    render(<CardTitle>Heading</CardTitle>);
    const heading = screen.getByRole('heading', { name: 'Heading' });
    expect(heading.tagName).toBe('H2');
  });

  it('merges custom classNames', () => {
    render(
      <Card className="extra" data-testid="card">
        x
      </Card>,
    );
    expect(screen.getByTestId('card')).toHaveClass('extra');
  });
});
