import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Routes, Route } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { SettingsPage } from './SettingsPage';

// Stub the heavy section components so the shell test stays isolated from
// their data dependencies — we only care about nav + routing here.
vi.mock('./sections/ProvidersSection', () => ({
  ProvidersSection: () => <div>Providers content</div>,
}));
vi.mock('./sections/TokensSection', () => ({
  TokensSection: () => <div>Tokens content</div>,
}));
vi.mock('./sections/WebhooksSection', () => ({
  WebhooksSection: () => <div>Webhooks content</div>,
}));
vi.mock('./sections/SystemSection', () => ({
  SystemSection: () => <div>System content</div>,
}));
vi.mock('./sections/DisplaySection', () => ({
  DisplaySection: () => <div>Display content</div>,
}));
vi.mock('./sections/AuditSection', () => ({
  AuditSection: () => <div>Audit content</div>,
}));
vi.mock('./sections/AboutSection', () => ({
  AboutSection: () => <div>About content</div>,
}));

// SettingsPage renders its own <Routes> for nested sections, so it must mount
// under a parent /settings/* route.
function renderAt(route: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/settings/*" element={<SettingsPage />} />
    </Routes>,
    { route },
  );
}

describe('SettingsPage', () => {
  it('renders the section navigation list', () => {
    renderAt('/settings');
    const nav = screen.getByRole('navigation', { name: /settings sections/i });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /providers/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /alerts/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /audit log/i })).toBeInTheDocument();
  });

  it('renders the matching section for a deep-linked route', () => {
    renderAt('/settings/system');
    expect(screen.getByText('System content')).toBeInTheDocument();
  });

  it('navigates to a section when a nav link is clicked', async () => {
    // On mobile the nav is the index list; clicking a link routes to the section.
    renderAt('/settings');
    await userEvent.click(screen.getByRole('link', { name: /token health/i }));
    expect(await screen.findByText('Tokens content')).toBeInTheDocument();
  });

  it('redirects unknown sub-paths back to the settings index', () => {
    renderAt('/settings/does-not-exist');
    // Falls back to the index; no section content rendered, nav still present.
    expect(screen.queryByText('Providers content')).not.toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: /settings sections/i })).toBeInTheDocument();
  });
});
