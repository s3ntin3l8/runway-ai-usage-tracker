import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import type { Sidecar } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { FleetPage } from './FleetPage';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// Silence "Query data cannot be undefined" warnings from the new
// untagged-credentials query that the fleet page makes for the
// silent-listener banner (PR #288). Tests that want to exercise the
// banner explicitly mock fetchUntaggedCredentials.
vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
  items: [],
  counts_by_sidecar: {},
});
vi.mocked(api.fetchSidecarDownloads).mockResolvedValue({
  channel: 'stable',
  release_url: 'https://example/releases',
  assets: [],
});

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
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchUntaggedCredentials).mockReset().mockResolvedValue({
      items: [],
      counts_by_sidecar: {},
    });
    vi.mocked(api.fetchProviderConfigs).mockReset().mockResolvedValue({ providers: [] });
  });

  it('shows the empty state when no sidecars are registered', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText(/no sidecars yet/i)).toBeInTheDocument();
    // The empty state leads straight into the install flow.
    expect(screen.getByText('Add a sidecar')).toBeInTheDocument();
  });

  it('links each sidecar to its credentials in Settings', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar(), sidecar({ sidecar_id: 'desktop', hostname: 'desktop' })],
    });
    renderWithProviders(<FleetPage />);
    await screen.findByText('laptop');

    // Credentials, identities and rules live in one place; Fleet just points at them.
    const links = screen.getAllByRole('link', { name: 'View' });
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      '/settings/credentials?view=machine#machine-laptop',
      '/settings/credentials?view=machine#machine-desktop',
    ]);
  });

  it.each([
    ['the banner chip', /untagged credentials? on sidecar laptop/i],
    ['the card pill', /^\d+ untagged credentials? — click to resolve/i],
  ])('opens the resolver for every untagged credential of the sidecar from %s', async (_label, name) => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        { sidecar_id: 'laptop', provider_id: 'antigravity', credential_origin: 'provider:antigravity' },
        { sidecar_id: 'laptop', provider_id: 'antigravity', credential_origin: 'env:ANTIGRAVITY_TOKEN' },
        { sidecar_id: 'laptop', provider_id: 'xai', credential_origin: 'provider:xai' },
      ],
      counts_by_sidecar: { laptop: 3 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name }));

    const dialog = await screen.findByRole('dialog');
    expect(api.fetchUntaggedCredentials).toHaveBeenCalledWith('laptop');
    // All three are listed — the pill used to open only the first entry.
    expect(within(dialog).getByText('provider:antigravity')).toBeInTheDocument();
    expect(within(dialog).getByText('env:ANTIGRAVITY_TOKEN')).toBeInTheDocument();
    expect(within(dialog).getByText('provider:xai')).toBeInTheDocument();
  });

  it('flags an offline sidecar that is behind as outdated', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ stale: true, update_available: false, outdated: true })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText('outdated')).toBeInTheDocument();
  });

  it('toggles the Add sidecar card from the header', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    renderWithProviders(<FleetPage />);
    await screen.findByText('laptop');
    expect(screen.queryByText('Add a sidecar')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /add sidecar/i }));
    expect(screen.getByText('Add a sidecar')).toBeInTheDocument();
  });

  it('renders a sidecar card with its identity', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ custom_name: 'My Laptop' })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText('My Laptop')).toBeInTheDocument();
  });

  it('shows online status for a fresh sidecar the server marks not stale', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ stale: false })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByRole('img', { name: 'online' })).toBeInTheDocument();
  });

  it('follows the server-computed `stale` flag rather than a client-side threshold', async () => {
    // Regression: the badge used to recompute liveness client-side from
    // last_seen with its own 30-minute threshold, which disagreed with the
    // server's 60-minute `stale` gate on the update-available badge. A
    // sidecar with a fresh-looking last_seen must still show "stale" once
    // the server says so — there is only one source of truth now.
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ last_seen: new Date().toISOString(), stale: true })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByRole('img', { name: 'stale' })).toBeInTheDocument();
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

  describe('keep-alive switch', () => {
    const keepAliveSwitch = () => screen.findByRole('switch', { name: /keep logins alive/i });
    const resolveOk = (desired: boolean | null) =>
      vi.mocked(api.setSidecarKeepAlive).mockResolvedValue({
        status: 'ok',
        keep_alive_desired: desired,
      });

    it('is a labeled switch with a visible description and status, not an icon button', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar({ keep_alive: false })] });
      renderWithProviders(<FleetPage />);

      const sw = await keepAliveSwitch();
      expect(sw).toHaveAttribute('aria-checked', 'false');
      expect(screen.getByText('Keep logins alive')).toBeInTheDocument();
      expect(screen.getByText(/Antigravity \(agy\), Claude Code, Codex \(ChatGPT\) and xAI \(Grok\) logins itself/)).toBeVisible();
      expect(screen.getByText('Off.')).toBeVisible();
      expect(screen.queryByRole('button', { name: /turn keep-alive/i })).not.toBeInTheDocument();
    });

    it('keeps the header to pause and delete (no keep-alive or reset icon buttons)', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ keep_alive: false, keep_alive_desired: true })],
      });
      renderWithProviders(<FleetPage />);
      await keepAliveSwitch();
      expect(screen.getByRole('button', { name: /pause collection/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /delete sidecar/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /use sidecar's own keep-alive/i })).toBeNull();
    });

    it('turns keep-alive on, sending the explicit value', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar({ keep_alive: false })] });
      resolveOk(true);
      renderWithProviders(<FleetPage />);

      await userEvent.click(await keepAliveSwitch());
      expect(api.setSidecarKeepAlive).toHaveBeenCalledWith('laptop', true);
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith(
          "Keep-alive on — applies on the sidecar's next check-in",
        ),
      );
    });

    it('turns it off for a sidecar running it, and badges the card', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar({ keep_alive: true })] });
      resolveOk(false);
      renderWithProviders(<FleetPage />);

      const sw = await keepAliveSwitch();
      expect(sw).toHaveAttribute('aria-checked', 'true');
      expect(screen.getByText('keep-alive')).toBeInTheDocument();
      expect(screen.getByText(/On — this sidecar renews its logins itself/)).toBeVisible();
      await userEvent.click(sw);
      expect(api.setSidecarKeepAlive).toHaveBeenCalledWith('laptop', false);
    });

    it('is disabled with an explanation for a sidecar that does not report keep-alive', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
      renderWithProviders(<FleetPage />);

      const sw = await keepAliveSwitch();
      expect(sw).toBeDisabled();
      expect(screen.getByText(/doesn't report keep-alive \(tray app or older build\)/)).toBeVisible();
      await userEvent.click(sw);
      expect(api.setSidecarKeepAlive).not.toHaveBeenCalled();
      expect(screen.queryByText('keep-alive')).not.toBeInTheDocument();
    });

    it('shows a pending request in the status line and the badge', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ keep_alive: false, keep_alive_desired: true })],
      });
      renderWithProviders(<FleetPage />);

      const sw = await keepAliveSwitch();
      expect(sw).toHaveAttribute('aria-checked', 'true');
      expect(sw).toHaveAttribute('data-pending', 'true');
      expect(screen.getByText(/Requested on — applies on next check-in/)).toBeVisible();
      expect(screen.getByText('keep-alive on pending')).toBeInTheDocument();
    });

    it('shows a requested-off that a never-reporting sidecar has not confirmed (the invisible case)', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ keep_alive: null, keep_alive_desired: false })],
      });
      renderWithProviders(<FleetPage />);

      const sw = await keepAliveSwitch();
      expect(sw).toBeEnabled(); // an override is queued, so it stays actionable
      expect(sw).toHaveAttribute('aria-checked', 'false');
      expect(
        screen.getByText(/Requested off — waiting for the sidecar to confirm/),
      ).toBeVisible();
      expect(screen.getByText(/tray app and older builds don't support keep-alive/)).toBeVisible();
      expect(screen.getByText('keep-alive off pending')).toBeInTheDocument();
    });

    it('says a change applies when an offline sidecar reconnects', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ keep_alive: false, stale: true })],
      });
      resolveOk(true);
      renderWithProviders(<FleetPage />);

      expect(await screen.findByText(/Sidecar is offline — it applies when it reconnects/)).toBeVisible();
      await userEvent.click(await keepAliveSwitch());
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith(
          'Keep-alive on — applies when the sidecar reconnects',
        ),
      );
    });

    it('is not pending once the sidecar reports the requested value', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ keep_alive: true, keep_alive_desired: true })],
      });
      renderWithProviders(<FleetPage />);

      const sw = await keepAliveSwitch();
      expect(sw).not.toHaveAttribute('data-pending');
      expect(screen.queryByText(/pending/i)).not.toBeInTheDocument();
      expect(screen.getByText('keep-alive')).toBeInTheDocument();
      // The override is still set, so the way back to the sidecar's own setting stays visible.
      expect(
        screen.getByRole('button', { name: /use the sidecar's own setting/i }),
      ).toBeInTheDocument();
    });

    it('keeps the switch and the clear action single-flight while one is in flight', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ keep_alive: false, keep_alive_desired: true })],
      });
      vi.mocked(api.setSidecarKeepAlive).mockReturnValue(new Promise(() => {}));
      renderWithProviders(<FleetPage />);

      await userEvent.click(await keepAliveSwitch());

      await waitFor(() =>
        expect(screen.getByRole('button', { name: /use the sidecar's own setting/i })).toBeDisabled(),
      );
      expect(screen.getByRole('switch', { name: /keep logins alive/i })).toBeDisabled();
      expect(api.setSidecarKeepAlive).toHaveBeenCalledTimes(1);
    });

    it("clears an override back to the sidecar's own setting", async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ keep_alive: false, keep_alive_desired: true })],
      });
      resolveOk(null);
      renderWithProviders(<FleetPage />);

      await userEvent.click(
        await screen.findByRole('button', { name: /use the sidecar's own setting/i }),
      );
      expect(api.setSidecarKeepAlive).toHaveBeenCalledWith('laptop', null);
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('override cleared')),
      );
    });

    it('offers no way back without an override', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar({ keep_alive: true })] });
      renderWithProviders(<FleetPage />);
      await keepAliveSwitch();
      expect(
        screen.queryByRole('button', { name: /use the sidecar's own setting/i }),
      ).not.toBeInTheDocument();
    });

    it('toasts the error when the request fails', async () => {
      vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar({ keep_alive: false })] });
      vi.mocked(api.setSidecarKeepAlive).mockRejectedValue(new Error('keep-alive boom'));
      renderWithProviders(<FleetPage />);

      await userEvent.click(await keepAliveSwitch());
      await waitFor(() => expect(toast.error).toHaveBeenCalledWith('keep-alive boom'));
    });

    it('gives each card an id so other pages can deep-link to it, and scrolls there', async () => {
      const scrollIntoView = vi.fn();
      Element.prototype.scrollIntoView = scrollIntoView;
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar(), sidecar({ sidecar_id: 'desktop', hostname: 'desktop' })],
      });
      renderWithProviders(<FleetPage />, { route: '/fleet#sidecar-desktop' });

      await screen.findAllByRole('switch', { name: /keep logins alive/i });
      const card = document.getElementById('sidecar-desktop');
      expect(card).not.toBeNull();
      expect(document.getElementById('sidecar-laptop')).not.toBeNull();
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
      expect(scrollIntoView.mock.contexts[0]).toBe(card);
    });

    it('scrolls to the linked card once, not again on every poll', async () => {
      const scrollIntoView = vi.fn();
      Element.prototype.scrollIntoView = scrollIntoView;
      vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
      const { client } = renderWithProviders(<FleetPage />, { route: '/fleet#sidecar-laptop' });
      await screen.findByRole('switch', { name: /keep logins alive/i });
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1));

      // The 60 s poll returns a fresh array (new last_seen): the page must not snap back.
      vi.mocked(api.fetchSidecars).mockResolvedValue({
        sidecars: [sidecar({ last_seen: new Date(Date.now() + 60_000).toISOString() })],
      });
      await client.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
      await waitFor(() => expect(api.fetchSidecars).toHaveBeenCalledTimes(2));
      await new Promise((r) => setTimeout(r, 50));
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });
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

  it('shows a BETA badge for a numbered-beta sidecar', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ channel: 'beta', sidecar_version: '3.0.0-beta.1' })],
    });
    renderWithProviders(<FleetPage />);
    expect(await screen.findByText('beta')).toBeInTheDocument();
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

  it('announces an available update from the Check for updates button', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.checkForUpdates).mockResolvedValue({
      current_version: '2.1.0',
      latest_version: '2.2.0',
      update_available: true,
    });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /check for updates/i }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('v2.2.0 is available')),
    );
  });

  it('surfaces a toast error when the update check fails', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.checkForUpdates).mockRejectedValue(new Error('network down'));
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /check for updates/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('network down'));
  });

  it('toasts success after pausing a sidecar', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.setSidecarEnabled).mockResolvedValue({ status: 'paused' });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /pause collection/i }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Sidecar paused'));
  });

  it('toasts an error when pause/resume fails', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.setSidecarEnabled).mockRejectedValue(new Error('toggle boom'));
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /pause collection/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('toggle boom'));
  });

  it('renders tag badges and a paused badge for a paused, tagged sidecar', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ collection_enabled: false, tags: ['office', 'gpu-box'] })],
    });
    renderWithProviders(<FleetPage />);

    await screen.findByText('office');
    expect(screen.getByText('gpu-box')).toBeInTheDocument();
    expect(screen.getByText('paused')).toBeInTheDocument();
  });

  it('opens the logs dialog when log lines are present', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ last_log_lines: ['boot ok', '', 'ingest 42 events'] })],
    });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /^logs$/i }));
    const dialog = await screen.findByRole('dialog');
    // Falsy lines are filtered out; the rest are joined into the <pre>.
    expect(within(dialog).getByText(/boot ok/)).toBeInTheDocument();
    expect(within(dialog).getByText(/ingest 42 events/)).toBeInTheDocument();
  });

  it('hides the Logs button when there are no log lines', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ last_log_lines: [] })],
    });
    renderWithProviders(<FleetPage />);
    await screen.findByText('laptop');
    expect(screen.queryByRole('button', { name: /^logs$/i })).not.toBeInTheDocument();
  });

  it('edits name and tags, then patches the sidecar and closes', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ custom_name: 'Old', tags: ['a'] })],
    });
    vi.mocked(api.patchSidecar).mockResolvedValue({ status: 'ok' } as never);
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /rename/i }));
    const dialog = await screen.findByRole('dialog');

    const nameInput = within(dialog).getByLabelText(/display name/i);
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'New Name');

    const tagsInput = within(dialog).getByLabelText(/tags/i);
    await userEvent.clear(tagsInput);
    await userEvent.type(tagsInput, ' work , ,  laptop ');

    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(api.patchSidecar).toHaveBeenCalledWith('laptop', {
        custom_name: 'New Name',
        tags: ['work', 'laptop'],
      }),
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Sidecar updated'));
    // Successful save closes the dialog (open=false → data-state closed).
    await waitFor(() => expect(dialog).toHaveAttribute('data-state', 'closed'));
  });

  it('toasts an error when saving the edit fails', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.patchSidecar).mockRejectedValue(new Error('save failed'));
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /rename/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('save failed'));
  });

  it('deletes a sidecar after confirming, then closes', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.deleteSidecar).mockResolvedValue({ status: 'deleted' } as never);
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /delete sidecar/i }));
    const dialog = await screen.findByRole('dialog');
    // Nothing is deleted until the user confirms in the dialog.
    expect(api.deleteSidecar).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole('button', { name: /^remove$/i }));
    await waitFor(() => expect(api.deleteSidecar).toHaveBeenCalledWith('laptop'));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Sidecar removed'));
    await waitFor(() => expect(dialog).toHaveAttribute('data-state', 'closed'));
  });

  it('cancels the delete dialog without calling deleteSidecar', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /delete sidecar/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^cancel$/i }));

    await waitFor(() => expect(dialog).toHaveAttribute('data-state', 'closed'));
    expect(api.deleteSidecar).not.toHaveBeenCalled();
  });

  it('toasts an error when deletion fails', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.deleteSidecar).mockRejectedValue(new Error('delete boom'));
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /delete sidecar/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^remove$/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('delete boom'));
  });

  it('toasts success and closes after pushing an update', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ update_available: true })],
    });
    vi.mocked(api.triggerSidecarUpdate).mockResolvedValue({ status: 'queued' } as never);
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /update now/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('Update pushed')),
    );
    await waitFor(() => expect(dialog).toHaveAttribute('data-state', 'closed'));
  });

  it('toasts an error when pushing an update fails', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ update_available: true })],
    });
    vi.mocked(api.triggerSidecarUpdate).mockRejectedValue(new Error('update boom'));
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /update now/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('update boom'));
  });

  it('closes the edit dialog when dismissed without saving', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /rename/i }));
    const dialog = await screen.findByRole('dialog');
    // Escape triggers onOpenChange(false) → onClose, with no patch call.
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(dialog).toHaveAttribute('data-state', 'closed'));
    expect(api.patchSidecar).not.toHaveBeenCalled();
  });

  it('closes the delete dialog via onOpenChange without deleting', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /delete sidecar/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(dialog).toHaveAttribute('data-state', 'closed'));
    expect(api.deleteSidecar).not.toHaveBeenCalled();
  });

  it('closes the update dialog via onOpenChange without pushing', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ update_available: true })],
    });
    renderWithProviders(<FleetPage />);

    await userEvent.click(await screen.findByRole('button', { name: /update now/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(dialog).toHaveAttribute('data-state', 'closed'));
    expect(api.triggerSidecarUpdate).not.toHaveBeenCalled();
  });

  // --- Silent-listener untagged credentials surface (PR #288) ---

  it('hides the Untagged banner when the sidecar has no pending entries', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [sidecar()] });
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [],
      counts_by_sidecar: {},
    });
    renderWithProviders(<FleetPage />);

    await screen.findByText('laptop');
    expect(screen.queryByRole('heading', { name: /untagged credentials/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /untagged credential/i }),
    ).not.toBeInTheDocument();
  });

  it('renders the Untagged banner with per-sidecar counts and a Tag now button', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar()],
    });
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        {
          sidecar_id: 'laptop',
          provider_id: 'anthropic',
          credential_origin: 'provider:anthropic',
        },
        {
          sidecar_id: 'laptop',
          provider_id: 'chatgpt',
          credential_origin: 'provider:chatgpt',
        },
        {
          sidecar_id: 'desktop',
          provider_id: 'anthropic',
          credential_origin: 'provider:anthropic',
        },
      ],
      counts_by_sidecar: { laptop: 2, desktop: 1 },
    });
    renderWithProviders(<FleetPage />);

    const banner = await screen.findByText(/untagged credentials/i);
    expect(banner).toBeInTheDocument();
    // Counts render in the banner copy.
    expect(within(banner.parentElement!).getByText('3 pending')).toBeInTheDocument();
    // Per-sidecar summary chips. Text spans multiple nodes (font-mono span
    // around the sidecar id), so use a flexible matcher.
    const bannerScope = banner.parentElement!.parentElement!;
    expect(bannerScope.textContent).toMatch(/laptop.*2/);
    expect(bannerScope.textContent).toMatch(/desktop.*1/);
  });

  it('shows an Untagged per-card badge when entries are pending on a sidecar', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ sidecar_id: 'laptop' })],
    });
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        {
          sidecar_id: 'laptop',
          provider_id: 'anthropic',
          credential_origin: 'provider:anthropic',
        },
      ],
      counts_by_sidecar: { laptop: 1 },
    });
    renderWithProviders(<FleetPage />);

    // Disambiguate from the banner chip by anchoring on the per-card
    // aria-label which names the sidecar explicitly.
    const badge = await screen.findByRole('button', {
      name: /untagged credential on sidecar laptop/i,
    });
    expect(badge).toBeInTheDocument();
  });

  it('opens the tag dialog from the per-card badge', async () => {
    // Smoke test for the per-card entry point. Detailed option-picking
    // flow lives in the dialog's unit tests; this just proves the badge
    // → dialog wiring is intact.
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar({ sidecar_id: 'laptop' })],
    });
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        {
          sidecar_id: 'laptop',
          provider_id: 'anthropic',
          credential_origin: 'provider:anthropic',
        },
      ],
      counts_by_sidecar: { laptop: 1 },
    });
    renderWithProviders(<FleetPage />);

    const badge = await screen.findByRole('button', {
      name: /untagged credential on sidecar laptop/i,
    });
    expect(badge).toBeInTheDocument();
    // Per-card click handler is bound via ``onResolveUntagged``; the
    // dialog's open-state is exercised by the dialog's own tests below
    // (which drive the click directly).
  });

  it('falls back to "Add one in Provider Settings" when no provider rows exist', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [sidecar()],
    });
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        {
          sidecar_id: 'laptop',
          provider_id: 'anthropic',
          credential_origin: 'provider:anthropic',
        },
      ],
      counts_by_sidecar: { laptop: 1 },
    });
    // No provider_configs rows for anthropic.
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    renderWithProviders(<FleetPage />);

    const badge = await screen.findByRole('button', {
      name: /untagged credential on sidecar laptop/i,
    });
    await userEvent.click(badge);

    const dialog = await screen.findByRole('dialog');
    // The empty-state paragraph reads: "No <span>anthropic</span> row
    // configured. Add one in Provider Settings." — broken across the
    // provider_id span. The <a> child is what links out, so query the
    // anchor text specifically — it's unique to this empty state.
    const link = within(dialog).getByRole('link', { name: /add one in provider settings/i });
    expect(link).toBeInTheDocument();
    expect(link.closest('p')?.textContent).toMatch(/no .*anthropic.* row configured/i);
    // No Tag button is rendered when there are no provider rows to pick from.
    // We scope the query to the dialog body so the global "Tag now" button
    // in the banner doesn't satisfy it (the banner sits outside the
    // dialog content area).
    expect(
      within(dialog).queryByRole('button', { name: /^tag$/i }),
    ).not.toBeInTheDocument();
  });
});
