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

describe('Tabs', () => {
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
});
