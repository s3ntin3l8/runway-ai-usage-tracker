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
vi.mock('./sections/credentials/CredentialsSection', () => ({
  CredentialsSection: () => <div>Credentials content</div>,
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
vi.mock('./sections/DataHealthSection', () => ({
  DataHealthSection: () => <div>Data health content</div>,
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

// The global test setup reports every media query as not matching, so the
// desktop branch (the persistent section pane) needs its own matchMedia.
const originalMatchMedia = window.matchMedia;
function installDesktopMatchMedia() {
  const mql = {
    matches: true,
    media: '(min-width: 1024px)',
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia;
}

describe('SettingsPage', () => {
  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('renders the section navigation list', () => {
    renderAt('/settings');
    const nav = screen.getByRole('navigation', { name: /settings sections/i });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /providers/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /alerts/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /audit log/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /data health/i })).toBeInTheDocument();
  });

  it('renders the matching section for a deep-linked route', () => {
    renderAt('/settings/system');
    expect(screen.getByText('System content')).toBeInTheDocument();
  });

  it('renders the Data health section for its deep-linked route', () => {
    renderAt('/settings/data-health');
    expect(screen.getByText('Data health content')).toBeInTheDocument();
  });

  it('navigates to a section when a nav link is clicked', async () => {
    // On mobile the nav is the index list; clicking a link routes to the section.
    renderAt('/settings');
    await userEvent.click(screen.getByRole('link', { name: /credentials/i }));
    expect(await screen.findByText('Credentials content')).toBeInTheDocument();
  });

  it('keeps old Token health deep links working by redirecting to Credentials', () => {
    renderAt('/settings/tokens');
    expect(screen.getByText('Credentials content')).toBeInTheDocument();
  });

  it('redirects unknown sub-paths back to the settings index', () => {
    renderAt('/settings/does-not-exist');
    // Falls back to the index; no section content rendered, nav still present.
    expect(screen.queryByText('Providers content')).not.toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: /settings sections/i })).toBeInTheDocument();
  });

  it('keeps the section pane beside the content on desktop', async () => {
    installDesktopMatchMedia();
    renderAt('/settings');
    // On desktop the index redirects to the first section, and the section
    // pane stays beside it.
    expect(await screen.findByText('Providers content')).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: /settings sections/i })).toBeInTheDocument();
    // The mobile index list is not rendered as well — one landmark, not two.
    expect(screen.getAllByRole('navigation', { name: /settings sections/i })).toHaveLength(1);
  });
});
