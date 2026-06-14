import { screen, within } from '@testing-library/react';
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

  it('shows the version in its own field', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ sidecar_version: '1.1.0+edge.899db312' })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText('Version')).toBeInTheDocument();
    expect(screen.getByText('v1.1.0+edge.899db312')).toBeInTheDocument();
  });

  it('hides Update now when no update is available', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ update_available: false })],
    });
    renderWithProviders(<FleetPage />);
    await screen.findByText('laptop');
    expect(screen.queryByRole('button', { name: /update now/i })).not.toBeInTheDocument();
  });

  it('confirms before pushing an update, then calls triggerSidecarUpdate', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ update_available: true })],
    });
    vi.mocked(api.triggerSidecarUpdate).mockResolvedValue({ status: 'queued' } as never);
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /update now/i }));
    // A confirm dialog opens; nothing is pushed until the user confirms.
    expect(api.triggerSidecarUpdate).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));
    expect(api.triggerSidecarUpdate).toHaveBeenCalledWith('laptop');
  });

  it('forces a release poll via the Check for updates header button', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.checkForUpdates).mockResolvedValue({
      current_version: '2.1.0',
      latest_version: '2.1.0',
      update_available: false,
    });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /check for updates/i }));
    expect(api.checkForUpdates).toHaveBeenCalled();
  });
});
