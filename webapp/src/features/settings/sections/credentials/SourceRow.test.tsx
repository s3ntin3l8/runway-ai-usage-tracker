import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { SourceRow } from './SourceRow';
import { source } from './testData';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const renderRow = (props: Parameters<typeof SourceRow>[0]) =>
  renderWithProviders(
    <ul>
      <SourceRow {...props} />
    </ul>,
  );

describe('SourceRow', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows where it came from, why it maps to the account, and its status', () => {
    renderRow({
      source: source({
        mapping: 'operator',
        mapping_scope: 'machine',
        token_types: ['oauth_token'],
        expires_in_seconds: 3 * 86_400,
      }),
    });
    expect(screen.getByText('oauth_creds.json')).toBeInTheDocument();
    expect(screen.getByText(/Workstation/)).toBeInTheDocument();
    expect(screen.getByText('Valid')).toBeInTheDocument();
    expect(screen.getByText(/OAuth · Assigned by you/)).toBeInTheDocument();
    expect(screen.getByText('in 3d')).toBeInTheDocument();
  });

  describe('keep-alive note', () => {
    const note = () => screen.getByText(/^keep-alive:/);

    it('shows "on" with no action, plus a link to the sidecar in Fleet', () => {
      renderRow({ source: source({ refreshed_by: 'machine', keep_alive: 'on' }) });
      expect(note()).toHaveTextContent('keep-alive: on');
      expect(screen.queryByRole('button', { name: /turn on keep-alive/i })).not.toBeInTheDocument();
      expect(screen.getByRole('link', { name: /open workstation in fleet/i })).toHaveAttribute(
        'href',
        '/fleet?settings=host-a',
      );
    });

    it('offers a Turn on button when it is off, and turns it on for that machine', async () => {
      vi.mocked(api.setSidecarKeepAlive).mockResolvedValue({
        status: 'ok',
        keep_alive_desired: true,
      });
      const { client } = renderRow({ source: source({ keep_alive: 'off' }) });
      const invalidate = vi.spyOn(client, 'invalidateQueries');
      expect(note()).toHaveTextContent('keep-alive: off');

      await userEvent.click(screen.getByRole('button', { name: /turn on keep-alive for workstation/i }));

      // Only this login's override: the machine's other logins keep following the sidecar.
      expect(api.setSidecarKeepAlive).toHaveBeenCalledWith('host-a', true, source().provider_id);
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith(
          "Keep-alive on — applies on the sidecar's next check-in",
        ),
      );
      // The shared hook refreshes the credentials views and the Fleet cards.
      await waitFor(() =>
        expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ['fleet'] })),
      );
    });

    it('keeps the full sentence in the tooltip while the row text stays compact', () => {
      renderRow({ source: source({ keep_alive: 'off', keep_alive_desired: true }) });
      expect(note()).toHaveAttribute(
        'title',
        expect.stringMatching(/Requested on — applies on next check-in/),
      );
    });

    it('still offers Turn on when an override says off and the sidecar agrees (nothing pending)', () => {
      renderRow({ source: source({ keep_alive: 'off', keep_alive_desired: false }) });
      expect(screen.getByRole('button', { name: /turn on keep-alive/i })).toBeInTheDocument();
    });

    it('shows a pending request instead of the button', () => {
      renderRow({ source: source({ keep_alive: 'off', keep_alive_desired: true }) });
      expect(note()).toHaveTextContent('keep-alive: requested on — pending');
      expect(screen.queryByRole('button', { name: /turn on keep-alive/i })).not.toBeInTheDocument();
    });

    it('is honest that a sidecar that never reports has not confirmed a request', () => {
      renderRow({ source: source({ keep_alive: 'unknown', keep_alive_desired: false }) });
      expect(note()).toHaveTextContent('keep-alive: requested off — unconfirmed');
      expect(screen.queryByRole('button', { name: /turn on keep-alive/i })).not.toBeInTheDocument();
    });

    it('says keep-alive is unavailable on an older sidecar, with no button but still a Fleet link', () => {
      renderRow({ source: source({ keep_alive: 'unknown' }) });
      expect(note()).toHaveTextContent('keep-alive: unavailable — update sidecar');
      expect(screen.queryByRole('button', { name: /turn on keep-alive/i })).not.toBeInTheDocument();
      expect(screen.getByRole('link', { name: /in fleet/i })).toBeInTheDocument();
    });

    it('tells the user a change on an offline machine applies when it reconnects', async () => {
      vi.mocked(api.setSidecarKeepAlive).mockResolvedValue({
        status: 'ok',
        keep_alive_desired: true,
      });
      renderRow({ source: source({ keep_alive: 'off', machine_stale: true }) });
      expect(note()).toHaveTextContent('machine offline, applies when it reconnects');

      await userEvent.click(screen.getByRole('button', { name: /turn on keep-alive/i }));
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith(
          'Keep-alive on — applies when the sidecar reconnects',
        ),
      );
    });

    it('toasts the error and keeps the button when the request fails', async () => {
      vi.mocked(api.setSidecarKeepAlive).mockRejectedValue(new Error('nope'));
      renderRow({ source: source({ keep_alive: 'off' }) });
      await userEvent.click(screen.getByRole('button', { name: /turn on keep-alive/i }));
      await waitFor(() =>
        expect(toast.error).toHaveBeenCalledWith('Could not turn keep-alive on: nope'),
      );
      expect(screen.getByRole('button', { name: /turn on keep-alive/i })).toBeEnabled();
    });

    it('shows nothing when keep-alive does not apply to the login', () => {
      renderRow({ source: source({ keep_alive: null }) });
      expect(screen.queryByText(/keep-alive/)).not.toBeInTheDocument();
      expect(screen.queryByRole('link', { name: /in fleet/i })).not.toBeInTheDocument();
    });

    it('has no Turn on or Fleet link for a row without a machine', () => {
      renderRow({ source: source({ keep_alive: 'off', machine_id: null, machine_name: null }) });
      expect(screen.queryByRole('button', { name: /turn on keep-alive/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('link', { name: /in fleet/i })).not.toBeInTheDocument();
    });
  });

  it('says a never-attempted credential has not been tried instead of implying it works', () => {
    renderRow({ source: source({ health: 'untried', last_success_at: null }) });
    expect(screen.getByText('not yet tried')).toBeInTheDocument();
  });

  it('marks the source behind the account data as active', () => {
    renderRow({ source: source({ is_active: true, last_success_at: new Date().toISOString() }) });
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('just now')).toBeInTheDocument();
  });

  it('reports a stale source by when it was last reported, not its stored expiry', () => {
    renderRow({
      source: source({
        status: 'stale',
        expires_in_seconds: -86_400,
        last_seen: new Date(Date.now() - 2 * 86_400_000).toISOString(),
      }),
    });
    expect(screen.getByText('Not reported')).toBeInTheDocument();
    expect(screen.getByText(/last reported 2d ago/)).toBeInTheDocument();
    expect(screen.queryByText(/expired/)).not.toBeInTheDocument();
  });

  it('shows the last collection error', () => {
    renderRow({ source: source({ status: 'invalid', last_error: 'Authentication failed' }) });
    expect(screen.getByText('Rejected')).toBeInTheDocument();
    expect(screen.getByText(/Last attempt: Authentication failed/)).toBeInTheDocument();
  });

  it('names the app that owns the file, not just the file name', () => {
    renderRow({
      source: source({
        label: 'auth.json',
        origin_app: 'Codex CLI',
        origin_path: '~/.codex/auth.json',
      }),
    });
    expect(screen.getByText('Codex CLI · auth.json')).toBeInTheDocument();
  });

  it('tells you how to re-authenticate a dead login on a live machine', () => {
    renderRow({
      source: source({
        status: 'expired',
        machine_name: 'mgmt',
        login_hint: 'run `codex login`',
        last_error: 'Authentication failed',
      }),
    });
    expect(screen.getByText('To fix: run `codex login` on mgmt.')).toBeInTheDocument();
  });

  it('suggests removal instead of re-login when the machine is offline', () => {
    renderRow({
      source: source({
        status: 'expired',
        machine_stale: true,
        login_hint: 'run `codex login`',
      }),
    });
    expect(screen.getByText('offline')).toBeInTheDocument();
    expect(screen.getByText(/remove this credential if the machine is retired/)).toBeInTheDocument();
    expect(screen.queryByText(/To fix/)).not.toBeInTheDocument();
  });

  it('does not nag about re-login for a healthy credential', () => {
    renderRow({ source: source({ login_hint: 'run `codex login`' }) });
    expect(screen.queryByText(/To fix/)).not.toBeInTheDocument();
  });

  it('warns about a login copied between machines, but not about a shared static key', () => {
    const { unmount } = renderRow({
      source: source({ rollable: true, shared_with: ['mgmt', 'macbook'] }),
    });
    expect(screen.getByText(/Same login also on mgmt, macbook/)).toBeInTheDocument();
    unmount();

    renderRow({ source: source({ rollable: false, shared_with: ['mgmt'] }) });
    expect(screen.queryByText(/Same login/)).not.toBeInTheDocument();
  });

  it('ignores a copy of the login on a machine that stopped checking in', () => {
    const { unmount } = renderRow({
      source: source({
        rollable: true,
        shared_with: ['mgmt', 'hermes-01'],
        shared_with_stale: ['hermes-01'],
      }),
    });
    // Only the live peer is named; the dead one can't sign anyone out.
    expect(screen.getByText(/Same login also on mgmt —/)).toBeInTheDocument();
    expect(screen.queryByText(/hermes-01/)).not.toBeInTheDocument();
    unmount();

    renderRow({
      source: source({ rollable: true, shared_with: ['hermes-01'], shared_with_stale: ['hermes-01'] }),
    });
    expect(screen.queryByText(/Same login/)).not.toBeInTheDocument();
  });

  it('does not warn on the offline machine\'s own row', () => {
    renderRow({
      source: source({ rollable: true, machine_stale: true, shared_with: ['mgmt'] }),
    });
    expect(screen.queryByText(/Same login/)).not.toBeInTheDocument();
  });

  it('shows the live re-test result, or says the row was not reached', () => {
    const { unmount } = renderRow({
      source: source(),
      probed: true,
      probe: { source_id: 'sidecar:a', outcome: 'auth_failed', probed: true, http_status: 401, duration_ms: 120 },
    });
    expect(screen.getByText('Rejected by the provider')).toBeInTheDocument();
    expect(screen.getByText('HTTP 401')).toBeInTheDocument();
    unmount();

    renderRow({ source: source(), probed: true });
    expect(screen.getByText('Not tested — no live credential.')).toBeInTheDocument();
  });

  it('gives each row\'s buttons a name that tells identical files apart', () => {
    renderRow({
      source: source({
        label: 'auth.json',
        origin_app: 'OpenCode',
        machine_name: 'dev-01',
        can_refresh: true,
      }),
      providerName: 'OpenRouter',
    });
    expect(
      screen.getByRole('button', { name: 'Remove OpenCode · auth.json on dev-01 (OpenRouter)' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Refresh OpenCode · auth.json on dev-01 (OpenRouter)' }),
    ).toBeInTheDocument();
  });

  it('warns when an all-machines rule covers an origin with no fingerprint', () => {
    renderRow({ source: source({ mapping: 'operator', mapping_scope: 'all_machines' }) });
    expect(screen.getByText(/applies on every machine/i)).toBeInTheDocument();
  });

  it('does not warn for a fingerprinted origin or a machine-scoped rule', () => {
    renderRow({
      source: source({ mapping: 'operator', mapping_scope: 'all_machines', fingerprinted: true }),
    });
    expect(screen.queryByText(/applies on every machine/i)).not.toBeInTheDocument();
  });

  it('shows config and server credentials without machine-only actions', () => {
    renderRow({
      source: source({
        origin_kind: 'server',
        label: 'GITHUB_TOKEN',
        machine_id: null,
        machine_name: null,
        mapping: 'server',
        removable: false,
      }),
    });
    expect(screen.getByText('GITHUB_TOKEN')).toBeInTheDocument();
    expect(screen.getByText('Server')).toBeInTheDocument();
    expect(screen.getByText(/Server environment/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /remove/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /refresh/i })).not.toBeInTheDocument();
  });

  it.each([
    ['provider_disabled', /Collection for this provider is turned off/],
    ['default_disabled', /default account is disabled/],
    ['account_keyed_config', /Add a "default" account to use it/],
    ['shadowed_by_config_key', /Settings → Providers takes precedence/],
  ] as const)('explains why a present server credential is unused (%s)', (reason, text) => {
    renderRow({
      source: source({
        origin_kind: 'server',
        label: 'GITHUB_TOKEN',
        machine_id: null,
        machine_name: null,
        mapping: 'server',
        removable: false,
        unused_reason: reason,
      }),
    });
    expect(screen.getByText('Not used')).toBeInTheDocument();
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it('does not flag a credential that is in use', () => {
    renderRow({ source: source({ unused_reason: null }) });
    expect(screen.queryByText('Not used')).not.toBeInTheDocument();
  });

  it("says a machine's rotating login is renewed by its machine and offers no refresh", () => {
    renderRow({
      source: source({ can_refresh: false, rollable: true, refreshed_by: 'machine' }),
    });

    expect(screen.getByText('renewed by its machine')).toBeInTheDocument();
    expect(screen.queryByText('auto-refreshed')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /refresh/i })).not.toBeInTheDocument();
  });

  it('refreshes this specific source', async () => {
    vi.mocked(api.postCredentialSourceRefresh).mockResolvedValue({ status: 'refreshed' });
    renderRow({ source: source({ can_refresh: true, rollable: true }) });

    expect(screen.getByText('auto-refreshed')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /refresh oauth_creds.json/i }));

    await waitFor(() =>
      expect(api.postCredentialSourceRefresh).toHaveBeenCalledWith(
        'gemini',
        'alice@example.com',
        'sidecar:a',
      ),
    );
    expect(toast.success).toHaveBeenCalledWith('Token refreshed');
  });

  it('surfaces a refresh failure', async () => {
    vi.mocked(api.postCredentialSourceRefresh).mockRejectedValue(new Error('boom'));
    renderRow({ source: source({ can_refresh: true }) });
    await userEvent.click(screen.getByRole('button', { name: /refresh/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Refresh failed: boom'));
  });

  it('asks for confirmation before removing, and does nothing on cancel', async () => {
    renderRow({ source: source() });
    await userEvent.click(screen.getByRole('button', { name: /remove oauth_creds.json/i }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/comes back on the machine's next report/i)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));

    expect(api.deleteCredentialSource).not.toHaveBeenCalled();
  });

  it('removes the source once confirmed', async () => {
    vi.mocked(api.deleteCredentialSource).mockResolvedValue({ ok: true });
    renderRow({ source: source() });
    await userEvent.click(screen.getByRole('button', { name: /remove oauth_creds.json/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^remove$/i }));

    await waitFor(() =>
      expect(api.deleteCredentialSource).toHaveBeenCalledWith(
        'gemini',
        'alice@example.com',
        'sidecar:a',
      ),
    );
    expect(toast.success).toHaveBeenCalledWith('Credential removed');
  });

  it('reports a failed removal and keeps the dialog usable', async () => {
    vi.mocked(api.deleteCredentialSource).mockRejectedValue(new Error('nope'));
    renderRow({ source: source() });
    await userEvent.click(screen.getByRole('button', { name: /remove oauth_creds.json/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^remove$/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('nope'));
  });

  it('shows the provider and account context in the by-machine view', () => {
    renderRow({ source: source(), context: 'Gemini · Work' });
    expect(screen.getByText(/Gemini · Work/)).toBeInTheDocument();
  });
});
