import { render } from '@testing-library/react';
import { Skeleton } from './Skeleton';

describe('Skeleton', () => {
  it('renders an animated, aria-hidden placeholder', () => {
    const { container } = render(<Skeleton />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toHaveClass('animate-pulse');
    expect(el).toHaveAttribute('aria-hidden');
  });

  it('merges a custom className', () => {
    const { container } = render(<Skeleton className="h-6 w-20" />);
    expect(container.firstElementChild).toHaveClass('h-6', 'w-20');
  });
});
