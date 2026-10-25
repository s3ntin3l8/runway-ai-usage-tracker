import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './Tabs';

function Harness() {
  return (
    <Tabs defaultValue="overview">
      <TabsList>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="events">Events</TabsTrigger>
      </TabsList>
      <TabsContent value="overview">Overview panel</TabsContent>
      <TabsContent value="events">Events panel</TabsContent>
    </Tabs>
  );
}

// jsdom has no layout, so the strip's and the triggers' boxes are stubbed.
// `scrollLeft` is stubbed too: jsdom's own setter is a no-op, and what these
// tests are about is where the strip was told to scroll to.
function box(left: number, width: number): DOMRect {
  return {
    left,
    right: left + width,
    top: 0,
    bottom: 44,
    width,
    height: 44,
    x: left,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

// The strip is `stripWidth` wide and starts at the viewport's left edge; the
// selected tab sits `tabLeft` into it and is `tabWidth` wide. Returns where
// the strip was told to scroll to.
function renderWithLayout(stripWidth: number, tabLeft: number, tabWidth: number) {
  const positions = new WeakMap<Element, number>();
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: Element,
  ) {
    const role = this.getAttribute('role');
    if (role === 'tablist') return box(0, stripWidth);
    if (role === 'tab') return box(tabLeft, tabWidth);
    return box(0, 0);
  });
  vi.spyOn(Element.prototype, 'scrollLeft', 'get').mockImplementation(function (this: Element) {
    return positions.get(this) ?? 0;
  });
  vi.spyOn(Element.prototype, 'scrollLeft', 'set').mockImplementation(function (
    this: Element,
    value: number,
  ) {
    positions.set(this, value);
  });
  render(<Harness />);
  return positions.get(screen.getByRole('tablist')) ?? 0;
}

describe('Tabs', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the triggers and the default panel', () => {
    render(<Harness />);
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Events' })).toBeInTheDocument();
    expect(screen.getByText('Overview panel')).toBeInTheDocument();
  });

  it('marks the default tab active', () => {
    render(<Harness />);
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute(
      'data-state',
      'active',
    );
  });

  it('switches panels when another tab is clicked', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('tab', { name: 'Events' }));
    expect(screen.getByText('Events panel')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Events' })).toHaveAttribute(
      'data-state',
      'active',
    );
  });

  it('scrolls the strip smoothly', () => {
    render(<Harness />);
    const list = screen.getByRole('tablist');
    expect(list).toHaveClass('overflow-x-auto');
    expect(list).toHaveClass('scroll-smooth');
  });

  it('fades the right edge only while tabs are scrolled off-screen', () => {
    render(<Harness />);
    const list = screen.getByRole('tablist');
    // jsdom reports no overflow, so nothing is hidden and nothing is faded.
    expect(list.className).not.toContain('mask-image');

    Object.defineProperty(list, 'scrollWidth', { value: 600, configurable: true });
    Object.defineProperty(list, 'clientWidth', { value: 300, configurable: true });
    fireEvent.scroll(list);
    expect(list.className).toContain('mask-image');

    // Scrolled to the end: the fade has nothing left to point at.
    Object.defineProperty(list, 'scrollLeft', { value: 300, configurable: true });
    fireEvent.scroll(list);
    expect(list.className).not.toContain('mask-image');
  });

  it('scrolls the strip so the selected tab is centred', () => {
    // The strip is the only thing that knows how wide its own content is, so
    // it is the one that brings the selected tab into view: a deep link names
    // a tab, and a tab off the edge of the strip is a tab the reader cannot
    // see. Here the selected tab sits 500px into a 300px strip, so the strip
    // scrolls until the tab is in the middle of it.
    expect(renderWithLayout(300, 500, 60)).toBe(500 - (300 - 60) / 2);
  });

  it('leaves the strip where it is when the selected tab is already on screen', () => {
    // A reader who scrolled the strip has chosen a position; it is not
    // undone by the tab that happens to be selected.
    expect(renderWithLayout(300, 100, 60)).toBe(0);
  });

  it('does nothing when the strip has no layout', () => {
    // jsdom reports no boxes at all, so there is nothing to reveal.
    render(<Harness />);
    const list = screen.getByRole('tablist');
    expect(list.scrollLeft).toBe(0);
  });
});
