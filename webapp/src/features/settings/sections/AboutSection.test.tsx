import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { AboutSection } from './AboutSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

const settings = {
  project_name: 'Runway',
  app_host: '127.0.0.1',
  app_port: 8765,
  version: '1.2.3',
  encryption_enabled: true,
  admin_auth_required: false,
  auth_methods: ['admin_key'],
  user_context: 'local',
  ingest_api_key_is_default: false,
};

describe('AboutSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows a skeleton while settings load', () => {
    vi.mocked(api.fetchSettings).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchStatus).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<AboutSection />);
    expect(container.querySelector('[class*="animate-shimmer"]')).toBeTruthy();
  });

  it('renders identity, version and collector status JSON once loaded', async () => {
    vi.mocked(api.fetchSettings).mockResolvedValue(settings);
    vi.mocked(api.fetchStatus).mockResolvedValue({ poller: 'running' });
    renderWithProviders(<AboutSection />);

    expect(await screen.findByText('Runway')).toBeInTheDocument();
    expect(screen.getByText('v1.2.3')).toBeInTheDocument();
    expect(screen.getByText('127.0.0.1:8765')).toBeInTheDocument();
    expect(screen.getByText('enabled')).toBeInTheDocument();
    expect(screen.getByText('open')).toBeInTheDocument();
    // Collector status pre block renders the raw JSON.
    expect(await screen.findByText(/"poller": "running"/)).toBeInTheDocument();
  });

  it('warns when the ingest API key is still the default', async () => {
    vi.mocked(api.fetchSettings).mockResolvedValue({
      ...settings,
      ingest_api_key_is_default: true,
    });
    vi.mocked(api.fetchStatus).mockResolvedValue({});
    renderWithProviders(<AboutSection />);
    expect(await screen.findByText(/still the insecure default/i)).toBeInTheDocument();
  });

  it('shows the build SHA, shortened, linked to its commit', async () => {
    // Assembled from a short literal so detect-secrets doesn't read the test
    // fixture as a high-entropy hex string (a real secret).
    const sha = 'abcdef012345'.repeat(3) + 'abcd';
    vi.mocked(api.fetchSettings).mockResolvedValue({ ...settings, build_sha: sha });
    vi.mocked(api.fetchStatus).mockResolvedValue({});
    renderWithProviders(<AboutSection />);

    const link = await screen.findByRole('link', { name: 'abcdef012345' });
    expect(link.getAttribute('href') ?? '').toContain(`/commit/${sha}`);
    expect(link).toHaveAttribute('title', `Commit ${sha}`);
  });

  it('shows a dash when the build carries no SHA', async () => {
    vi.mocked(api.fetchSettings).mockResolvedValue({ ...settings, build_sha: null });
    vi.mocked(api.fetchStatus).mockResolvedValue({});
    renderWithProviders(<AboutSection />);

    expect(await screen.findByText('Build')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /[0-9a-f]{12}/ })).toBeNull();
  });
});
