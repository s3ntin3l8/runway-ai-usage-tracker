import { render } from '@testing-library/react';
import { ProviderGlyph } from './ProviderGlyph';

describe('ProviderGlyph', () => {
  it('renders a brand-mark image for a known provider', () => {
    const { container } = render(<ProviderGlyph providerId="anthropic" />);
    const img = container.querySelector('img');
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute('src');
  });

  it('renders a monogram tile for an unknown provider', () => {
    const { container } = render(<ProviderGlyph providerId="unknownco" name="Unknown Co" />);
    expect(container.querySelector('img')).toBeNull();
    // first two letters of the name, uppercased
    expect(container.textContent).toBe('UN');
  });

  it('falls back to providerId when name is absent', () => {
    const { container } = render(<ProviderGlyph providerId="zz-custom" />);
    expect(container.textContent).toBe('ZZ');
  });

  it('produces a deterministic tile color for the same id', () => {
    const a = render(<ProviderGlyph providerId="repeatable" />).container.firstElementChild;
    const b = render(<ProviderGlyph providerId="repeatable" />).container.firstElementChild;
    expect(a?.className).toBe(b?.className);
  });

  it('merges a custom className', () => {
    const { container } = render(
      <ProviderGlyph providerId="unknownco" className="my-glyph" />,
    );
    expect(container.firstElementChild).toHaveClass('my-glyph');
  });
});
