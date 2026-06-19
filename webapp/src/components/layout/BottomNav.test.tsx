import { screen, within } from '@testing-library/react';
import { makeQueryClient, renderWithProviders } from '@/test/utils';
import { BottomNav } from './BottomNav';
import { NAV_ITEMS } from './nav';

describe('BottomNav', () => {
  it('renders a labelled nav with every item linked to its target', () => {
    renderWithProviders(<BottomNav />);
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    for (const item of NAV_ITEMS) {
      const link = within(nav).getByRole('link', { name: item.label });
      expect(link).toHaveAttribute('href', item.to);
    }
  });

  it('applies the active accent class to the current route', () => {
    renderWithProviders(<BottomNav />, { route: '/history' });
    expect(screen.getByRole('link', { name: 'History' })).toHaveClass('text-accent');
  });

  it('shows a sr-only update notice on the Fleet tab when sidecars have updates', () => {
    const client = makeQueryClient();
    client.setQueryData(['fleet', 'sidecars'], {
      sidecars: [
        { sidecar_id: 'a', update_available: true },
        { sidecar_id: 'b', update_available: false },
      ],
    });
    renderWithProviders(<BottomNav />, { client });
    expect(screen.getByText('1 sidecar updates available')).toBeInTheDocument();
  });

  it('does not show an update notice on the Fleet tab when no sidecars have updates', () => {
    const client = makeQueryClient();
    client.setQueryData(['fleet', 'sidecars'], {
      sidecars: [{ sidecar_id: 'a', update_available: false }],
    });
    renderWithProviders(<BottomNav />, { client });
    expect(screen.queryByText(/sidecar update/i)).not.toBeInTheDocument();
  });
});
