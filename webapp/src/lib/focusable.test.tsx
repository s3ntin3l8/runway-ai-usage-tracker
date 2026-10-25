import { markFocusable, rendersFocusableElement } from './focusable';

const Marked = markFocusable(function Marked() {
  return null;
});

describe('rendersFocusableElement', () => {
  it('recognises natively focusable host tags', () => {
    expect(rendersFocusableElement(<button />)).toBe(true);
    expect(rendersFocusableElement(<a href="/x" />)).toBe(true);
    expect(rendersFocusableElement(<input />)).toBe(true);
  });

  it('treats role-less markup as not focusable', () => {
    expect(rendersFocusableElement(<span />)).toBe(false);
    expect(rendersFocusableElement(<div />)).toBe(false);
  });

  it('respects an explicit tabindex', () => {
    expect(rendersFocusableElement(<span tabIndex={0} />)).toBe(true);
    // Programmatically focusable, but not reachable by keyboard: not ours to
    // change.
    expect(rendersFocusableElement(<span tabIndex={-1} />)).toBe(false);
  });

  it('recognises a component that renders its own focusable element', () => {
    expect(rendersFocusableElement(<Marked />)).toBe(true);
    expect(rendersFocusableElement(<Marked />)).toBe(true);
  });

  it('ignores non-elements', () => {
    expect(rendersFocusableElement('plain text')).toBe(false);
    expect(rendersFocusableElement(null)).toBe(false);
  });
});
