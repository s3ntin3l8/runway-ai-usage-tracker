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
    expect(screen.getByText('Assigned by you')).toBeInTheDocument();
    expect(screen.getByText('oauth_token')).toBeInTheDocument();
    expect(screen.getByText('in 3d')).toBeInTheDocument();
  });

  it('marks the source behind the account data as active', () => {
    renderRow({ source: source({ is_active: true, last_success_at: new Date().toISOString() }) });
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText(/collected just now/)).toBeInTheDocument();
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
    expect(screen.getByText('· Server')).toBeInTheDocument();
    expect(screen.getByText('Server environment')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /remove/i })).not.toBeInTheDocument();
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
