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
});
