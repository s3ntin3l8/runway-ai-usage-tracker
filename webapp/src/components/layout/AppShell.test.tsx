import { screen } from '@testing-library/react';
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
});
