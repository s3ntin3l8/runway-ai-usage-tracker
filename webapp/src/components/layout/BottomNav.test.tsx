import { screen, within } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
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
});
