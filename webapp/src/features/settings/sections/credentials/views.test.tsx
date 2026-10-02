import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { ByMachineView } from './ByMachineView';
import { ByProviderView } from './ByProviderView';
import { NeedsMappingView } from './NeedsMappingView';
import { RulesView } from './RulesView';
import { account, inventory, multiMachineInventory, source } from './testData';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
// The unassigned-usage card and tag dialog have their own suites.
vi.mock('@/features/fleet/PendingUsageEventsCard', () => ({
  PendingUsageEventsCard: () => <div>Pending usage card</div>,
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchSidecars).mockResolvedValue({
    sidecars: [
      { sidecar_id: 'host-a', hostname: 'host-a', custom_name: 'Workstation' },
    ] as never,
  });
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
});

describe('ByProviderView', () => {
  it('lists the same credential once per machine under one account', () => {
    // The original token-health bug: four machines, one visible row.
    renderWithProviders(<ByProviderView providers={multiMachineInventory().providers} />);

    expect(screen.getByRole('heading', { name: 'Gemini' })).toBeInTheDocument();
    expect(screen.getByText('Work')).toBeInTheDocument();
    expect(screen.getByText('3 credentials')).toBeInTheDocument();
    const list = screen.getByRole('list', { name: /Work credentials/i });
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    expect(within(list).getByText(/DEV-01/)).toBeInTheDocument();
    expect(within(list).getByText(/MacBook/)).toBeInTheDocument();
    expect(within(list).getByText(/mgmt/)).toBeInTheDocument();
  });

  it('says where the account data comes from', () => {
    renderWithProviders(<ByProviderView providers={multiMachineInventory().providers} />);
    expect(
      screen.getByText(/Data from oauth_creds\.json on DEV-01 · api \/ sidecar · collected just now/),
    ).toBeInTheDocument();
  });

  it('says so when no collection has succeeded yet', () => {
    renderWithProviders(
      <ByProviderView
        providers={[{ provider_id: 'gemini', name: 'Gemini', accounts: [account([source()])] }]}
      />,
    );
    expect(screen.getByText('No successful collection recorded yet')).toBeInTheDocument();
  });

  it('names an unidentified account instead of showing a placeholder id', () => {
    renderWithProviders(
      <ByProviderView
        providers={[
          {
            provider_id: 'gemini',
            name: 'Gemini',
            accounts: [
              account([source({ account_id: 'default', identity_pending: true, mapping: 'pending' })], {
                account_id: 'default',
                identity_pending: true,
                status: 'unknown',
              }),
            ],
          },
        ]}
      />,
    );
    expect(screen.getByText('Needs an account', { selector: 'p' })).toBeInTheDocument();
  });
});

describe('ByMachineView', () => {
  it('groups credentials under the machine that reported them', () => {
    renderWithProviders(<ByMachineView inventory={multiMachineInventory()} />);

    expect(screen.getByRole('heading', { name: 'DEV-01' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'MacBook' })).toBeInTheDocument();
    const list = screen.getByRole('list', { name: 'DEV-01 credentials' });
    expect(within(list).getByText(/Gemini · Work/)).toBeInTheDocument();
  });

  it('links a machine with unmapped credentials to the Needs mapping view', () => {
    renderWithProviders(<ByMachineView inventory={multiMachineInventory()} />);
    const link = screen.getByRole('link', { name: /MacBook: 2 need mapping/i });
    expect(link).toHaveAttribute('href', '/?view=mapping');
  });

  it('says so when a machine reports nothing', () => {
    renderWithProviders(
      <ByMachineView
        inventory={inventory({
          machines: [
            { machine_id: 'idle', name: 'Idle box', last_seen: null, credential_count: 0, unmapped_count: 0 },
          ],
        })}
      />,
    );
    expect(screen.getByText('This machine reports no credentials.')).toBeInTheDocument();
  });

  it('labels credentials whose account is still unknown', () => {
    const pending = account(
      [source({ account_id: 'default', identity_pending: true, mapping: 'pending' })],
      { account_id: 'default', identity_pending: true },
    );
    renderWithProviders(
      <ByMachineView
        inventory={inventory({
          providers: [{ provider_id: 'gemini', name: 'Gemini', accounts: [pending] }],
          machines: [
            { machine_id: 'host-a', name: 'Workstation', last_seen: null, credential_count: 1, unmapped_count: 0 },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Gemini · needs an account/)).toBeInTheDocument();
  });
});

describe('NeedsMappingView', () => {
  const entry = {
    sidecar_id: 'host-a',
    provider_id: 'chatgpt',
    credential_origin: 'path:/home/u/.codex/auth.json',
    first_seen: new Date().toISOString(),
    claimed_account_id: 'alice@example.com',
  };

  it('celebrates an empty queue', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({ items: [], counts_by_sidecar: {} });
    renderWithProviders(<NeedsMappingView pendingUsageEvents={0} />);
    expect(await screen.findByText('Every credential has an account')).toBeInTheDocument();
    expect(screen.queryByText('Pending usage card')).not.toBeInTheDocument();
  });

  it('lists credentials with the machine name and any claimed identity', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { 'host-a': 1 },
    });
    renderWithProviders(<NeedsMappingView pendingUsageEvents={0} />);

    const list = await screen.findByRole('list', { name: 'Credentials without an account' });
    expect(within(list).getByText('path:/home/u/.codex/auth.json')).toBeInTheDocument();
    expect(await within(list).findByText(/on Workstation/)).toBeInTheDocument();
    expect(within(list).getByText(/claims alice@example.com/)).toBeInTheDocument();
    // One credential: no bulk action.
    expect(screen.queryByRole('button', { name: /assign all/i })).not.toBeInTheDocument();
  });

  it('offers to assign all when several are waiting, and shows unassigned usage', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry, { ...entry, credential_origin: 'env:CHATGPT_TOKEN' }],
      counts_by_sidecar: { 'host-a': 2 },
    });
    renderWithProviders(<NeedsMappingView pendingUsageEvents={4} />);

    expect(await screen.findByRole('button', { name: 'Assign all (2)' })).toBeInTheDocument();
    expect(screen.getByText('Pending usage card')).toBeInTheDocument();
  });

  it('opens the resolver for the chosen credential', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { 'host-a': 1 },
    });
    renderWithProviders(<NeedsMappingView pendingUsageEvents={0} />);

    await userEvent.click(await screen.findByRole('button', { name: 'Assign account' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/chatgpt · path:\/home\/u\/\.codex\/auth\.json/)).toBeInTheDocument();
  });
});

describe('NeedsMappingView deep link', () => {
  const entry = {
    sidecar_id: 'host-a',
    provider_id: 'deepseek',
    credential_origin: 'env:DEEPSEEK_API_KEY',
    first_seen: new Date().toISOString(),
    claimed_account_id: null,
  };
  const other = { ...entry, provider_id: 'chatgpt', credential_origin: 'env:CHATGPT_TOKEN' };

  it('opens the dialog for the origin named in the link, and only that one', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [other, entry],
      counts_by_sidecar: { 'host-a': 2 },
    });
    renderWithProviders(<NeedsMappingView pendingUsageEvents={0} />, {
      route:
        '/settings/credentials?view=mapping&sidecar=host-a&provider=deepseek&origin=env%3ADEEPSEEK_API_KEY',
    });

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/deepseek · env:DEEPSEEK_API_KEY/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/chatgpt/)).not.toBeInTheDocument();
  });

  it('matches a fingerprinted origin by the link\'s plain one', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [{ ...entry, credential_origin: 'env:DEEPSEEK_API_KEY#86e40eb64385' }],
      counts_by_sidecar: { 'host-a': 1 },
    });
    renderWithProviders(<NeedsMappingView pendingUsageEvents={0} />, {
      route:
        '/settings/credentials?view=mapping&sidecar=host-a&provider=deepseek&origin=env%3ADEEPSEEK_API_KEY',
    });
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('stays closed when the origin is no longer waiting', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [other],
      counts_by_sidecar: { 'host-a': 1 },
    });
    renderWithProviders(<NeedsMappingView pendingUsageEvents={0} />, {
      route:
        '/settings/credentials?view=mapping&sidecar=host-a&provider=deepseek&origin=env%3ADEEPSEEK_API_KEY',
    });

    expect(await screen.findByText('env:CHATGPT_TOKEN')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('RulesView', () => {
  const rule = {
    provider_id: 'anthropic',
    credential_origin: 'provider:anthropic',
    account_id: 'team@example.com',
    sidecar_id: 'host-a',
    set_by: 'operator',
    set_at: null,
  };

  it('shows each rule with the machine it applies to', async () => {
    vi.mocked(api.fetchCredentialTags).mockResolvedValue({
      items: [rule, { ...rule, credential_origin: 'env:X', sidecar_id: null }],
    });
    renderWithProviders(<RulesView />);

    const list = await screen.findByRole('list', { name: 'Assignment rules' });
    expect(await within(list).findByText('on Workstation')).toBeInTheDocument();
    expect(within(list).getByText('on all machines')).toBeInTheDocument();
  });

  it('explains an empty rule list', async () => {
    vi.mocked(api.fetchCredentialTags).mockResolvedValue({ items: [] });
    renderWithProviders(<RulesView />);
    expect(await screen.findByText('No assignment rules')).toBeInTheDocument();
  });

  it('shows a load failure instead of silently vanishing', async () => {
    vi.mocked(api.fetchCredentialTags).mockRejectedValue(new Error('backend down'));
    renderWithProviders(<RulesView />);
    expect(await screen.findByText(/Couldn't load rules: backend down/)).toBeInTheDocument();
  });

  it('confirms before removing a rule', async () => {
    vi.mocked(api.fetchCredentialTags).mockResolvedValue({ items: [rule] });
    vi.mocked(api.deleteCredentialTag).mockResolvedValue({ ok: true } as never);
    renderWithProviders(<RulesView />);

    await userEvent.click(await screen.findByRole('button', { name: /remove rule/i }));
    const dialog = await screen.findByRole('dialog');
    expect(api.deleteCredentialTag).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole('button', { name: /^remove$/i }));

    await waitFor(() => expect(api.deleteCredentialTag).toHaveBeenCalledWith(rule));
    expect(toast.success).toHaveBeenCalledWith('Rule removed');
  });

  it('reports a failed removal', async () => {
    vi.mocked(api.fetchCredentialTags).mockResolvedValue({ items: [rule] });
    vi.mocked(api.deleteCredentialTag).mockRejectedValue(new Error('denied'));
    renderWithProviders(<RulesView />);

    await userEvent.click(await screen.findByRole('button', { name: /remove rule/i }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^remove$/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('denied'));
  });
});
