import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Sidecar } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { FleetPage } from './FleetPage';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const sidecar = (o: Partial<Sidecar> = {}): Sidecar => ({
  sidecar_id: 'laptop',
  hostname: 'laptop',
  last_seen: new Date().toISOString(),
  ingest_count: 10,
  error_count: 0,
  sidecar_version: '1.0.0',
  collection_enabled: true,
  ...o,
});

describe('FleetPage', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the empty state when no sidecars are registered', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText(/no sidecars yet/i)).toBeInTheDocument();
  });

  it('renders a sidecar card with its identity', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ custom_name: 'My Laptop' })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText('My Laptop')).toBeInTheDocument();
  });

  it('pauses an active sidecar via the pause control', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.setSidecarEnabled).mockResolvedValue({ status: 'paused' });
    renderWithProviders(<FleetPage />);

    const btn = await screen.findByRole('button', { name: /pause collection/i });
    await userEvent.click(btn);
    // setSidecarEnabled(id, enabled): an active card passes its current
    // (un-paused) state → false → the pause endpoint.
    expect(api.setSidecarEnabled).toHaveBeenCalledWith('laptop', false);
  });

  it('marks a paused sidecar and offers resume', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ collection_enabled: false })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByRole('button', { name: /resume collection/i })).toBeInTheDocument();
  });

  it('exposes Rename / tags as a button', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByRole('button', { name: /rename/i })).toBeInTheDocument();
  });

  it('shows an EDGE badge for an edge-channel sidecar', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ channel: 'edge' })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText('edge')).toBeInTheDocument();
  });

  it('omits the EDGE badge for a stable-channel sidecar', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ channel: 'stable' })],
    });
    renderWithProviders(<FleetPage />);
    await screen.findByText('laptop');
    expect(screen.queryByText('edge')).not.toBeInTheDocument();
  });
});
