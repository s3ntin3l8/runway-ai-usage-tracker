import { screen, within } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
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
});
