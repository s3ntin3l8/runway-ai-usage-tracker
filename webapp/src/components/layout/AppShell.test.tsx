import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { AppShell } from './AppShell';

describe('AppShell', () => {
  it('renders the sidebar, bottom nav, and the routed outlet content', () => {
    renderWithProviders(
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<div>outlet content</div>} />
        </Route>
      </Routes>,
    );
    // Both navs share the "Primary" label.
    expect(screen.getAllByRole('navigation', { name: 'Primary' })).toHaveLength(2);
    expect(screen.getByText('outlet content')).toBeInTheDocument();
    expect(screen.getByText('Runway')).toBeInTheDocument();
  });

  it('offers a skip link to the main content', async () => {
    // WCAG 2.4.1: without it, a keyboard user tabs through the whole primary
    // nav on every page load before reaching any content.
    renderWithProviders(
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<div>outlet content</div>} />
        </Route>
      </Routes>,
    );
    const skip = screen.getByRole('link', { name: /skip to main content/i });
    expect(skip).toHaveAttribute('href', '#main');
    // The target exists and is focusable, so the jump moves the focus.
    const main = screen.getByRole('main');
    expect(main).toHaveAttribute('id', 'main');
    expect(main).toHaveAttribute('tabindex', '-1');

    await userEvent.tab();
    expect(skip).toHaveFocus();
  });
});
