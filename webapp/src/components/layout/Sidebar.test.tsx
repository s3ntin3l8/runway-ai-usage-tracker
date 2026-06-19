import { screen, within } from '@testing-library/react';
import { makeQueryClient, renderWithProviders } from '@/test/utils';
import { NAV_ITEMS } from './nav';
import { Sidebar } from './Sidebar';

describe('Sidebar', () => {
  it('renders the brand and every nav item with its target', () => {
    renderWithProviders(<Sidebar />);
    expect(screen.getByText('Runway')).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    for (const item of NAV_ITEMS) {
      const link = within(nav).getByRole('link', { name: item.label });
      expect(link).toHaveAttribute('href', item.to);
    }
  });

  it('marks the active route link', () => {
    renderWithProviders(<Sidebar />, { route: '/fleet' });
    const link = screen.getByRole('link', { name: 'Fleet' });
    expect(link).toHaveClass('bg-surface-3');
  });

  it('shows a warning count pill on the Fleet link when sidecars have updates', () => {
    const client = makeQueryClient();
    client.setQueryData(['fleet', 'sidecars'], {
      sidecars: [
        { sidecar_id: 'a', update_available: true },
        { sidecar_id: 'b', update_available: true },
        { sidecar_id: 'c', update_available: false },
      ],
    });
    renderWithProviders(<Sidebar />, { client });
    // Badge renders as a <span title="..."> — query by title.
    expect(screen.getByTitle('2 sidecar updates available')).toBeInTheDocument();
  });

  it('does not show a fleet update badge when no sidecars have updates', () => {
    const client = makeQueryClient();
    client.setQueryData(['fleet', 'sidecars'], {
      sidecars: [{ sidecar_id: 'a', update_available: false }],
    });
    renderWithProviders(<Sidebar />, { client });
    expect(screen.queryByTitle(/sidecar update/i)).not.toBeInTheDocument();
  });
});
