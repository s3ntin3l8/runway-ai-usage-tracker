import { render } from '@testing-library/react';
import { RunwayMark } from './RunwayMark';

describe('RunwayMark', () => {
  it('renders an svg glyph and forwards the className', () => {
    const { container } = render(<RunwayMark className="size-6" />);
    const svg = container.querySelector('svg');
    expect(svg).toBeTruthy();
    expect(svg).toHaveClass('size-6');
    expect(svg).toHaveAttribute('aria-hidden');
  });
});
