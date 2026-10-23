import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { FleetSourceStatus } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { SidecarSources, SidecarSourceNotice } from './SidecarSources';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
const sidecarId = 'device-' + 'a'.repeat(32);
const source: FleetSourceStatus = {
  source_id: 'anthropic.claude-code',
  provider_id: 'anthropic',
  label: 'Claude Code',
  revision: 'a'.repeat(64),
  generation: 1,
  read: true,
  state: 'Limited',
  renewal_requested: false,
  renewal_supported: false,
  renewal_capable: true,
  new: true,
  remote_paused: false,
  revoked: false,
  report_at: '2026-10-22T12:00:00Z',
  report_stale: true,
  announcement: true,
  collection_health: 'healthy',
  last_collection_at: '2026-10-23T12:00:00Z',
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchSidecarSources).mockResolvedValue({
    protocol: 1,
    sources: [source],
    commands: [],
  });
  vi.mocked(api.commandSidecarSource).mockResolvedValue({
    id: 1,
    status: 'pending',
  });
});
it('shows stale reports separately from healthy collection and requests local setup', async () => {
  const user = userEvent.setup();
  renderWithProviders(
    <SidecarSources osPlatform="Darwin/26" sidecarId={sidecarId} />,
  );
  expect(await screen.findByText('Claude Code')).toBeInTheDocument();
  expect(screen.getByText(/Server collection: healthy/)).toBeInTheDocument();
  expect(screen.getByText(/Stale device report/)).toBeInTheDocument();
  expect(
    screen.getByText(/supported write path not verified/),
  ).toBeInTheDocument();
  expect(api.commandSidecarSource).not.toHaveBeenCalled();
  await user.click(
    screen.getByRole('button', { name: 'Request local setup for Claude Code' }),
  );
  await waitFor(() =>
    expect(api.commandSidecarSource).toHaveBeenCalledWith(
      sidecarId,
      source.source_id,
      'request_setup',
    ),
  );
});
it('local Off prevents remotely resuming a pause', async () => {
  vi.mocked(api.fetchSidecarSources).mockResolvedValue({
    protocol: 1,
    sources: [{ ...source, read: false, remote_paused: true, state: 'Off' }],
    commands: [],
  });
  renderWithProviders(
    <SidecarSources osPlatform="Darwin/26" sidecarId={sidecarId} />,
  );
  expect(
    await screen.findByRole('button', {
      name: 'Resume server pause for Claude Code',
    }),
  ).toBeDisabled();
});
it('pauses and revokes only the selected contribution and dismisses its revision notice', async () => {
  const user = userEvent.setup();
  renderWithProviders(
    <SidecarSources osPlatform="Darwin/26" sidecarId={sidecarId} />,
  );
  await user.click(
    await screen.findByRole('button', { name: 'Pause Claude Code' }),
  );
  await waitFor(() =>
    expect(api.commandSidecarSource).toHaveBeenCalledWith(
      sidecarId,
      source.source_id,
      'pause',
    ),
  );
  await user.click(
    screen.getByRole('button', { name: 'Revoke Claude Code contribution' }),
  );
  await waitFor(() =>
    expect(api.commandSidecarSource).toHaveBeenCalledWith(
      sidecarId,
      source.source_id,
      'revoke',
    ),
  );
  await user.click(
    screen.getByRole('button', { name: 'Dismiss notice for Claude Code' }),
  );
  await waitFor(() =>
    expect(api.commandSidecarSource).toHaveBeenCalledWith(
      sidecarId,
      source.source_id,
      'dismiss',
    ),
  );
});
it('does not query legacy devices and shows limited compatibility', () => {
  renderWithProviders(
    <SidecarSources osPlatform="Darwin/26" sidecarId="legacy-host" />,
  );
  expect(screen.getByText(/pair this sidecar again/)).toBeInTheDocument();
  expect(api.fetchSidecarSources).not.toHaveBeenCalled();
});
it('shows queued commands and source errors without granting permission', async () => {
  vi.mocked(api.fetchSidecarSources).mockResolvedValue({
    protocol: 1,
    sources: [{ ...source, revoked: true, collection_health: 'withdrawn' }],
    commands: [
      {
        id: 1,
        source_id: source.source_id,
        generation: 1,
        action: 'request_setup',
      },
    ],
  });
  renderWithProviders(
    <SidecarSources osPlatform="Darwin/26" sidecarId={sidecarId} />,
  );
  expect(
    await screen.findByText(/Awaiting local acknowledgment: request setup/),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'Revoke Claude Code contribution' }),
  ).toBeDisabled();
});
it('banner opens details and never enables a scraper', async () => {
  const user = userEvent.setup();
  const onOpen = vi.fn();
  renderWithProviders(
    <SidecarSourceNotice
      osPlatform="Darwin/26"
      sidecarId={sidecarId}
      onOpen={onOpen}
    />,
  );
  await user.click(
    await screen.findByRole('button', { name: 'Review Keychain scrapers' }),
  );
  expect(onOpen).toHaveBeenCalledOnce();
  expect(api.commandSidecarSource).not.toHaveBeenCalled();
});
it('dismissed notices remain hidden until the server reports a new revision', async () => {
  vi.mocked(api.fetchSidecarSources).mockResolvedValue({
    protocol: 1,
    sources: [{ ...source, announcement: false }],
    commands: [],
  });
  renderWithProviders(
    <SidecarSourceNotice
      osPlatform="Darwin/26"
      sidecarId={sidecarId}
      onOpen={vi.fn()}
    />,
  );
  await waitFor(() => expect(api.fetchSidecarSources).toHaveBeenCalled());
  expect(
    screen.queryByRole('button', { name: 'Review Keychain scrapers' }),
  ).not.toBeInTheDocument();
});

it('does not target Windows or unknown devices with Keychain announcements', () => {
  renderWithProviders(
    <SidecarSourceNotice
      sidecarId={sidecarId}
      osPlatform="Windows/11"
      onOpen={vi.fn()}
    />,
  );
  expect(api.fetchSidecarSources).not.toHaveBeenCalled();
  expect(
    screen.queryByText('New Keychain scraper—opt in now'),
  ).not.toBeInTheDocument();
});
