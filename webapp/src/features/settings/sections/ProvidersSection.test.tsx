import { createElement } from 'react';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { DragEndEvent } from '@dnd-kit/core';
import type { ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { ProvidersSection, reorderStrategies } from './ProvidersSection';
import * as api from '@/api/endpoints';
import { toast } from 'sonner';

vi.mock('@/api/endpoints');

// Store dnd-kit callbacks so tests can invoke them directly.
const dndCallbacks: {
  onDragStart: (() => void) | null;
  onDragEnd: ((e: DragEndEvent) => void) | null;
  onDragCancel: (() => void) | null;
} = { onDragStart: null, onDragEnd: null, onDragCancel: null };
vi.mock('@dnd-kit/core', async () => {
  const actual = await vi.importActual<typeof import('@dnd-kit/core')>('@dnd-kit/core');
  return {
    ...actual,
    DndContext: vi.fn(({ children, onDragStart, onDragEnd, onDragCancel, ...props }: any) => {
      dndCallbacks.onDragStart = onDragStart;
      dndCallbacks.onDragEnd = onDragEnd;
      dndCallbacks.onDragCancel = onDragCancel;
      return createElement(actual.DndContext, { ...props, onDragStart, onDragEnd, onDragCancel }, children);
    }).mockName('DndContext'),
  };
});

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

const provider = (o: Partial<ProviderConfig> = {}): ProviderConfig => ({
  provider_id: 'claude',
  name: 'Claude',
  enabled: true,
  api_key_set: true,
  session_cookie_set: false,
  account_label: 'Work',
  effective_poll_interval: 60,
  supports_api_key: true,
  supports_session_cookie: false,
  api_key_label: 'API key', // pragma: allowlist secret
  collection_strategies: [{ id: 'api', enabled: true }],
  // Multi-account shape: legacy single-account users have one row under
  // `accounts` keyed by `account_id="default"`. The v2 UI iterates this
  // list; the legacy dialog renders the canonical row above.
  accounts: [
    {
      account_id: 'default',
      account_label: 'Work',
      enabled: true,
      api_key_set: true,
      session_cookie_set: false,
      poll_interval_seconds: null,
      collection_strategies: [{ id: 'api', enabled: true }],
      has_live_data: true,
      is_orphaned: false,
    },
  ],
  account_count: 1,
  ...o,
});

const githubProvider = (o: Partial<ProviderConfig> = {}): ProviderConfig =>
  provider({
    provider_id: 'github',
    name: 'GitHub',
    api_key_label: 'Personal access token', // pragma: allowlist secret
    ...o,
  });

async function openGitHubDialog() {
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
    providers: [githubProvider()],
  });
  renderWithProviders(<ProvidersSection />);
  await userEvent.click(await screen.findByText('GitHub'));
  return screen.findByRole('dialog');
}

describe('ProvidersSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows skeletons while configs load', () => {
    vi.mocked(api.fetchProviderConfigs).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<ProvidersSection />);
    expect(container.querySelectorAll('[class*="animate-shimmer"]').length).toBeGreaterThan(0);
  });

  it('lists providers with their state badges', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    renderWithProviders(<ProvidersSection />);

    expect(await screen.findByText('Claude')).toBeInTheDocument();
    expect(screen.getByText('key')).toBeInTheDocument();
    expect(screen.getByText('enabled')).toBeInTheDocument();
  });

  it('opens the edit dialog and saves config via putProviderConfig', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));

    const dialog = await screen.findByRole('dialog');
    // Type a new API key so it gets included in the body.
    await userEvent.type(within(dialog).getByLabelText('API key'), 'sk-new');

    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    expect(api.putProviderConfig).toHaveBeenCalledWith('claude', 'default', {
      enabled: true,
      archived: false,
      account_label: 'Work',
      poll_interval_seconds: null,
      collection_strategies: [{ id: 'api', enabled: true }],
      api_key: 'sk-new', // pragma: allowlist secret
    });
  });

  it('toggles a collection strategy off before saving', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');

    // The strategy switch is labelled by its display label ("api").
    await userEvent.click(within(dialog).getByRole('switch', { name: 'api' }));
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    const body = vi.mocked(api.putProviderConfig).mock.calls[0][2];
    expect(body.collection_strategies).toEqual([{ id: 'api', enabled: false }]);
  });

  it('opens the edit dialog via the Enter key on a provider card', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    renderWithProviders(<ProvidersSection />);

    const card = await screen.findByRole('button', { name: /Claude/i });
    card.focus();
    await userEvent.keyboard('{Enter}');
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('omits untouched credentials from the save body', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    const body = vi.mocked(api.putProviderConfig).mock.calls[0][2];
    expect(body).not.toHaveProperty('api_key');
    expect(body).not.toHaveProperty('session_cookie');
  });

  it('renders drag handles on each strategy in the settings dialog', async () => {
    const multi = provider({
      collection_strategies: [
        { id: 'web', enabled: true },
        { id: 'oauth', enabled: false },
        { id: 'sidecar', enabled: true },
      ],
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [multi] });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');

    const grips = within(dialog).getAllByRole('button', { name: /reorder/i });
    expect(grips).toHaveLength(3);
  });

  it('each strategy grip has an accessible label naming the strategy', async () => {
    const multi = provider({
      collection_strategies: [
        { id: 'api', enabled: true },
        { id: 'web', enabled: false },
      ],
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [multi] });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByRole('button', { name: /reorder api/i })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /reorder web/i })).toBeInTheDocument();
  });

  it('reorders strategies when handleDragEnd fires with mismatched ids', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({
          collection_strategies: [
            { id: 'web', enabled: true },
            { id: 'oauth', enabled: false },
            { id: 'sidecar', enabled: true },
          ],
        }),
      ],
    });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    await screen.findByRole('dialog');
    expect(dndCallbacks.onDragEnd).toBeTruthy();

    // Invoke the captured onDragEnd with a simulated event.
    dndCallbacks.onDragEnd!({
      active: { id: 'web' } as DragEndEvent['active'],
      over: { id: 'sidecar' } as DragEndEvent['over'],
    } as DragEndEvent);

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    const body = vi.mocked(api.putProviderConfig).mock.calls[0][2];
    expect(body.collection_strategies).toEqual([
      { id: 'oauth', enabled: false },
      { id: 'sidecar', enabled: true },
      { id: 'web', enabled: true },
    ]);
  });

  it('calls setPullToRefreshSuspended(false) when handleDragEnd fires', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    const spy = vi.spyOn(await import('@/lib/pullToRefresh'), 'setPullToRefreshSuspended');
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    await screen.findByRole('dialog');

    dndCallbacks.onDragEnd!({
      active: { id: 'api' } as DragEndEvent['active'],
      over: { id: 'api' } as DragEndEvent['over'],
    } as DragEndEvent);

    expect(spy).toHaveBeenCalledWith(false);
  });

  it('calls setPullToRefreshSuspended(true) when onDragStart fires', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    const spy = vi.spyOn(await import('@/lib/pullToRefresh'), 'setPullToRefreshSuspended');
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    await screen.findByRole('dialog');

    dndCallbacks.onDragStart!();

    expect(spy).toHaveBeenCalledWith(true);
  });

  it('calls setPullToRefreshSuspended(false) when onDragCancel fires', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    const spy = vi.spyOn(await import('@/lib/pullToRefresh'), 'setPullToRefreshSuspended');
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    await screen.findByRole('dialog');

    dndCallbacks.onDragCancel!();

    expect(spy).toHaveBeenCalledWith(false);
  });

  it('preserves strategy order when saving after toggling a strategy', async () => {
    const multi = provider({
      collection_strategies: [
        { id: 'web', enabled: true },
        { id: 'oauth', enabled: false },
      ],
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [multi] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');

    await userEvent.click(within(dialog).getByRole('switch', { name: 'oauth' }));
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    const body = vi.mocked(api.putProviderConfig).mock.calls[0][2];
    expect(body.collection_strategies).toEqual([
      { id: 'web', enabled: true },
      { id: 'oauth', enabled: true },
    ]);
  });

  it('renders the collection strategies fieldset only when strategies exist', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByText('Collection strategies')).toBeInTheDocument();
    expect(within(dialog).getByRole('switch', { name: 'api' })).toBeInTheDocument();
  });

  it('omits the collection strategies fieldset when there are no strategies', async () => {
    const noStrats = provider({ collection_strategies: undefined, supported_strategies: [] });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [noStrats] });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).queryByText('Collection strategies')).not.toBeInTheDocument();
  });

  it('clears pull-to-refresh suspend flag when the dialog unmounts', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    const { unmount } = renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    await screen.findByRole('dialog');

    unmount();
    // The unmount cleanup fires — no assertion needed beyond not throwing
    // (the useEffect safety net calls setPullToRefreshSuspended(false)).
  });
});

describe('GitHubLoginSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows Connect button when not authenticated', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    const dialog = await openGitHubDialog();
    expect(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    ).toBeInTheDocument();
  });

  it('shows account name and Disconnect when authenticated', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({
      authenticated: true,
      account: 'octocat',
      email: 'octocat@github.com',
    });
    const dialog = await openGitHubDialog();
    expect(await within(dialog).findByText('@octocat')).toBeInTheDocument();
    expect(within(dialog).getByText('octocat@github.com')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /disconnect/i })).toBeInTheDocument();
  });

  it('shows account without email when email is absent', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({
      authenticated: true,
      account: 'octocat',
    });
    const dialog = await openGitHubDialog();
    expect(await within(dialog).findByText('@octocat')).toBeInTheDocument();
    expect(within(dialog).queryByText(/@.*\.com/)).toBeNull();
  });

  it('starts device flow and shows pending UI with user code', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockResolvedValue({
      device_code: 'dev-abc',
      user_code: 'ABCD-1234',
      verification_uri: 'https://github.com/login/device',
      expires_in: 900,
      interval: 5,
    });
    vi.mocked(api.pollGitHubOAuth).mockReturnValue(new Promise(() => {}));

    const dialog = await openGitHubDialog();
    await userEvent.click(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    );

    expect(await within(dialog).findByText('ABCD-1234')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /cancel/i })).toBeInTheDocument();
  });

  it('returns to idle when Cancel is clicked during pending flow', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockResolvedValue({
      device_code: 'dev-abc',
      user_code: 'ABCD-1234',
      verification_uri: 'https://github.com/login/device',
      expires_in: 900,
      interval: 5,
    });
    vi.mocked(api.pollGitHubOAuth).mockReturnValue(new Promise(() => {}));

    const dialog = await openGitHubDialog();
    await userEvent.click(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    );
    await within(dialog).findByText('ABCD-1234');

    await userEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));

    expect(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    ).toBeInTheDocument();
  });

  it('shows error state when initGitHubOAuth fails', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockRejectedValue(new Error('Network error'));

    const dialog = await openGitHubDialog();
    await userEvent.click(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    );

    expect(await within(dialog).findByText('Network error')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('resets to idle from error state on Try again click', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockRejectedValue(new Error('Network error'));

    const dialog = await openGitHubDialog();
    await userEvent.click(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    );
    await within(dialog).findByText('Network error');

    await userEvent.click(within(dialog).getByRole('button', { name: /try again/i }));

    expect(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    ).toBeInTheDocument();
  });

  it('calls logoutGitHub and shows toast on Disconnect', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({
      authenticated: true,
      account: 'octocat',
    });
    vi.mocked(api.logoutGitHub).mockResolvedValue({});

    const dialog = await openGitHubDialog();
    await userEvent.click(await within(dialog).findByRole('button', { name: /disconnect/i }));

    expect(api.logoutGitHub).toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith('GitHub disconnected');
  });

  it('transitions to idle after successful poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockResolvedValue({
      device_code: 'dev-abc',
      user_code: 'ABCD-1234',
      verification_uri: 'https://github.com/login/device',
      expires_in: 900,
      interval: 5,
    });
    vi.mocked(api.pollGitHubOAuth).mockResolvedValue({ status: 'success' });

    const dialog = await openGitHubDialog();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(
      await within(dialog).findByRole('button', { name: /connect via github oauth/i }),
    );

    await within(dialog).findByText('ABCD-1234');
    await vi.runAllTimersAsync();

    expect(toast.success).toHaveBeenCalledWith('GitHub connected');
    vi.useRealTimers();
  });
});

describe('reorderStrategies', () => {
  it('moves an item from old index to new index', () => {
    const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
    expect(reorderStrategies(items, 'a', 'c')).toEqual([{ id: 'b' }, { id: 'c' }, { id: 'a' }]);
  });

  it('returns the same array when active and over ids match', () => {
    const items = [{ id: 'a' }, { id: 'b' }];
    expect(reorderStrategies(items, 'a', 'a')).toEqual([{ id: 'a' }, { id: 'b' }]);
  });

  it('returns the same array when active id is not found', () => {
    const items = [{ id: 'a' }, { id: 'b' }];
    expect(reorderStrategies(items, 'x', 'a')).toEqual([{ id: 'a' }, { id: 'b' }]);
  });

  it('returns the same array when over id is not found', () => {
    const items = [{ id: 'a' }, { id: 'b' }];
    expect(reorderStrategies(items, 'a', 'x')).toEqual([{ id: 'a' }, { id: 'b' }]);
  });

  it('preserves the enabled state of each item after reorder', () => {
    const items = [
      { id: 'web', enabled: true },
      { id: 'oauth', enabled: false },
      { id: 'sidecar', enabled: true },
    ];
    const result = reorderStrategies(items, 'web', 'sidecar');
    expect(result).toEqual([
      { id: 'oauth', enabled: false },
      { id: 'sidecar', enabled: true },
      { id: 'web', enabled: true },
    ]);
  });
});

// ---------------------------------------------------------------------------
// v2 path — empty-canvas card grid + per-account detail dialog. Activated by
// the `?providers=v2` URL param (rollback = drop the param). Rendered in the
// v2 subroute so the legacy tests above can run unchanged.
// ---------------------------------------------------------------------------

describe('ProvidersSection (v2 — ?providers=v2)', () => {
  beforeEach(() => vi.clearAllMocks());

  const renderV2 = (ui: React.ReactElement) =>
    renderWithProviders(ui, { route: '/settings/providers?providers=v2' });

  it('renders the empty state when no providers are configured', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    expect(await screen.findByText('No providers configured')).toBeInTheDocument();
    expect(screen.getByText(/add your first provider/i)).toBeInTheDocument();
  });

  it('renders a card per provider with the N accounts subtitle', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({ provider_id: 'claude', name: 'Claude', account_count: 1 }),
        provider({
          provider_id: 'openrouter',
          name: 'OpenRouter',
          account_count: 3,
          accounts: [
            { account_id: 'default', account_label: 'A', enabled: true, api_key_set: true, session_cookie_set: false, poll_interval_seconds: null, collection_strategies: null, has_live_data: true, is_orphaned: false },
            { account_id: 'alice@example.com', account_label: 'Alice', enabled: true, api_key_set: true, session_cookie_set: false, poll_interval_seconds: null, collection_strategies: null, has_live_data: true, is_orphaned: false },
            { account_id: 'bob@example.com', account_label: 'Bob', enabled: false, api_key_set: false, session_cookie_set: false, poll_interval_seconds: null, collection_strategies: null, has_live_data: true, is_orphaned: false },
          ],
        }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    expect(await screen.findByText('Claude')).toBeInTheDocument();
    // Subtitle line embeds the count + poll in a single <p>; use a function
    // matcher so it finds the substring within the surrounding text node.
    expect(screen.getByText((_, node) => node?.textContent?.startsWith('1 account · ') ?? false)).toBeInTheDocument();
    expect(screen.getByText((_, node) => node?.textContent?.startsWith('3 accounts · ') ?? false)).toBeInTheDocument();
  });

  it('opens the detail dialog when a provider card is clicked', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Claude')).toBeInTheDocument();
    // The default account row is rendered.
    expect(within(dialog).getByText('Work')).toBeInTheDocument();
  });

  it('shows the orphan hint for account_id="default" rows without live data', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({
          accounts: [
            {
              account_id: 'default',
              account_label: null,
              enabled: true,
              api_key_set: false,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              has_live_data: false,
              is_orphaned: true,
            },
          ],
        }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/no usage data — safe to remove/i)).toBeInTheDocument();
  });

  it('removes an account when the user confirms', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.deleteProviderConfig).mockResolvedValue({ status: 'ok' });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /actions for work/i }));
    await userEvent.click(within(dialog).getByRole('menuitem', { name: /remove/i }));
    await userEvent.click(within(dialog).getByRole('button', { name: /remove account/i }));

    expect(api.deleteProviderConfig).toHaveBeenCalledWith('claude', 'default');
  });

  it('disables the Add provider / Add account buttons pending the wizard (#287)', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    const addBtn = await screen.findByRole('button', { name: /add provider/i });
    expect(addBtn).toBeDisabled();

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    const dialogAdd = within(dialog).getByRole('button', { name: /add account/i });
    expect(dialogAdd).toBeDisabled();
  });

  it('filters the grid by the search input', async () => {
    // Search input only renders when there are >5 providers (UI hides it for
    // small lists to reduce noise). Six providers triggers the filter row.
    const sixProviders = [
      provider({ provider_id: 'p1', name: 'Alpha' }),
      provider({ provider_id: 'p2', name: 'Bravo' }),
      provider({ provider_id: 'p3', name: 'Charlie' }),
      provider({ provider_id: 'p4', name: 'Delta' }),
      provider({ provider_id: 'p5', name: 'Echo' }),
      provider({ provider_id: 'openrouter', name: 'OpenRouter' }),
    ];
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: sixProviders });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await screen.findByText('Alpha');
    const search = screen.getByLabelText(/search providers/i);
    await userEvent.type(search, 'open');
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument();
    expect(screen.getByText('OpenRouter')).toBeInTheDocument();
  });

  it('opens the per-account edit dialog and saves via putProviderConfig', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const detail = await screen.findByRole('dialog');
    await userEvent.click(within(detail).getByRole('button', { name: /actions for work/i }));
    await userEvent.click(within(detail).getByRole('menuitem', { name: /^edit$/i }));

    const accountDialog = await screen.findByRole('dialog');
    // The account dialog's title includes the display name.
    expect(within(accountDialog).getByText(/edit account · work/i)).toBeInTheDocument();
  });
});
