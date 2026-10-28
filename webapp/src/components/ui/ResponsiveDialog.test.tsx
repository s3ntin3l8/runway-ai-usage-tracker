import { render, screen } from '@testing-library/react';
import { ResponsiveDialog } from './ResponsiveDialog';

// jsdom has no real viewport: force the desktop (Radix dialog) branch.
vi.mock('@/hooks/useMediaQuery', () => ({ useIsDesktop: () => true }));

describe('ResponsiveDialog (desktop)', () => {
  it('caps the dialog to the viewport and scrolls only the body', () => {
    render(
      <ResponsiveDialog open onOpenChange={() => {}} title="Settings" description="A tall one">
        <div data-testid="body">lots of content</div>
      </ResponsiveDialog>,
    );

    const dialog = screen.getByRole('dialog', { name: 'Settings' });
    // A tall dialog must not run off the screen: it is a column capped to the viewport...
    expect(dialog.className).toMatch(/max-h-\[calc\(100dvh-2rem\)\]/);
    expect(dialog.className).toMatch(/\bflex-col\b/);
    // ...whose body scrolls while the title stays in view.
    const scroller = screen.getByTestId('body').parentElement!;
    expect(scroller.className).toMatch(/overflow-y-auto/);
    expect(scroller.className).toMatch(/min-h-0/);
    expect(dialog).toContainElement(screen.getByText('Settings'));
    expect(scroller).not.toContainElement(screen.getByText('Settings'));
  });
});
