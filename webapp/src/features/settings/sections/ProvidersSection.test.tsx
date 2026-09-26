import { createElement } from 'react';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { DragEndEvent } from '@dnd-kit/core';
import type { ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { ProvidersSection, reorderItems } from './ProvidersSection';
import * as api from '@/api/endpoints';

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
  // Existing single-account users have one row under `accounts` keyed by
  // `account_id="default"`; the grid renders that account as a card.
  accounts: [
    {
      account_id: 'default',
      account_label: 'Work',
      enabled: true,
      api_key_set: true,
      session_cookie_set: false,
      poll_interval_seconds: null,
      collection_strategies: [{ id: 'api', enabled: true }],
      is_orphaned: false,
    },
  ],
  account_count: 1,
  ...o,
});

// Provider settings multi-account grid tests.

describe('ProvidersSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('reorders items by id while preserving their data', () => {
    const items = [
      { id: 'api', enabled: true },
      { id: 'web', enabled: false },
    ];
    expect(reorderItems(items, 'api', 'web')).toEqual([
      { id: 'web', enabled: false },
      { id: 'api', enabled: true },
    ]);
  });

  const renderV2 = (ui: React.ReactElement) =>
    renderWithProviders(ui, { route: '/settings/providers' });

  it('renders the empty state when no providers are configured', async () => {
    // `/provider-configs` enumerates the registry on every fresh install,
    // so the response carries N providers all with `account_count: 0`.
    // Mocking `{ providers: [] }` would pin a shape the server never
    // produces (Hermes round-2).
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [provider({ account_count: 0, accounts: [] })],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    expect(await screen.findByText('No providers configured')).toBeInTheDocument();
    expect(screen.getByText(/add your first provider/i)).toBeInTheDocument();
    // Exactly one Add provider affordance on a fresh install: the
    // EmptyState's own button. The sticky footer Add is gated on
    // `hasAnyConfig` (ProvidersSection.tsx:340) and stays hidden here so
    // users don't see two identical CTAs.
    expect(screen.getAllByRole('button', { name: /add provider/i })).toHaveLength(1);
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
            { account_id: 'default', account_label: 'A', enabled: true, api_key_set: true, session_cookie_set: false, poll_interval_seconds: null, collection_strategies: null, is_orphaned: false },
            { account_id: 'alice@example.com', account_label: 'Alice', enabled: true, api_key_set: true, session_cookie_set: false, poll_interval_seconds: null, collection_strategies: null, is_orphaned: false },
            { account_id: 'bob@example.com', account_label: 'Bob', enabled: false, api_key_set: false, session_cookie_set: false, poll_interval_seconds: null, collection_strategies: null, is_orphaned: false },
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

  it('shows the orphan hint for account_id="default" rows without live data when a live sibling exists', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({
          // Server contract: is_orphaned is only true when the default row
          // is shadowed by a sibling with live data. Mirror that here.
          accounts: [
            {
              account_id: 'default',
              account_label: null,
              enabled: true,
              api_key_set: false,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: true,
            },
            {
              account_id: 'alice@example.com',
              account_label: 'Alice',
              enabled: true,
              api_key_set: true,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false,
            },
          ],
          account_count: 2,
        }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/no usage data — safe to remove/i)).toBeInTheDocument();
  });

  it('does not show the orphan hint for default rows that have live data', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({
          accounts: [
            {
              account_id: 'default',
              account_label: null,
              enabled: true,
              api_key_set: true,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false, // <-- live data present, server flipped the flag
            },
          ],
        }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByText(/no usage data — safe to remove/i)).not.toBeInTheDocument();
  });

  it('does not show the orphan hint for non-default rows without live data', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({
          accounts: [
            {
              account_id: 'alice@example.com', // not "default"
              account_label: 'New account',
              enabled: true,
              api_key_set: true,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false, // <-- only `default` rows can be orphaned
            },
          ],
        }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByText(/no usage data — safe to remove/i)).not.toBeInTheDocument();
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

  it('opens the wizard when Add provider is clicked (#287)', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    const addBtn = await screen.findByRole('button', { name: /add provider/i });
    await userEvent.click(addBtn);

    // Wizard opens at step 1 with the provider catalog visible — find the
    // step-1 heading ("Add provider · step 1 of 3").
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(await screen.findByText(/Add provider · step 1 of 3/)).toBeInTheDocument();
  });

  it('opens the wizard pre-scoped when Add account is clicked in the detail dialog (#287)', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const detail = await screen.findByRole('dialog');
    await userEvent.click(within(detail).getByRole('button', { name: /add account/i }));

    // Pre-scoped: the wizard skips step 1 and shows step 2 (Credentials)
    // directly. The dialog title is set via Radix Dialog.Title so we can
    // locate it by its accessible name.
    const wizard = await screen.findByRole('dialog');
    expect(wizard).toHaveAccessibleName(/Add account · Claude · step 2 of 3/);
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

  it('keeps an open detail dialog in sync after an invalidate (#294 Bug 2A)', async () => {
    // Initial snapshot: one enabled account.
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [provider({ provider_id: 'gemini', name: 'Gemini', account_count: 1 })],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderV2(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Gemini'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('enabled')).toBeInTheDocument();

    // Toggle fires putProviderConfig, which invalidates provider-configs.
    // Second fetch returns the updated (disabled) snapshot.
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({
          provider_id: 'gemini',
          name: 'Gemini',
          account_count: 1,
          accounts: [
            {
              account_id: 'default',
              account_label: null,
              enabled: false,
              api_key_set: true,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false,
            },
          ],
        }),
      ],
    });

    const master = within(dialog).getByRole('switch', { name: /all accounts enabled/i });
    await userEvent.click(master);
    await waitFor(() => expect(api.putProviderConfig).toHaveBeenCalled());
    // Dialog derives from configs.data each render — must re-sync to disabled.
    await waitFor(() => expect(within(dialog).getByText('disabled')).toBeInTheDocument());
    expect(within(dialog).queryByText('enabled')).not.toBeInTheDocument();
  });

  it('shows an "auto" badge for discovered-only accounts (#294 Bug 3)', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({
          provider_id: 'antigravity',
          name: 'Antigravity',
          account_count: 1,
          accounts: [
            {
              account_id: 'user@example.com',
              account_label: 'user@example.com',
              enabled: true,
              api_key_set: false,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false,
              source: 'discovered',
            },
          ],
        }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    expect(await screen.findByText('Antigravity')).toBeInTheDocument();
    // Discovered-only → "auto", never "unconfigured".
    expect(screen.getByText('auto')).toBeInTheDocument();
    expect(screen.queryByText('unconfigured')).not.toBeInTheDocument();
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

  it('persists provider reorder via putDashboardLayout on drag end (#286)', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({ provider_id: 'a', name: 'Alpha' }),
        provider({ provider_id: 'b', name: 'Bravo' }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({
      provider_order: ['a', 'b'],
      card_orders: {},
    });
    vi.mocked(api.putDashboardLayout).mockResolvedValue({ status: 'ok' });
    renderV2(<ProvidersSection />);

    await screen.findByText('Alpha');

    // The mocked DndContext captures the onDragEnd callback. Simulate a drop
    // that moves 'a' past 'b' → expect the reordered list and the PUT body.
    expect(dndCallbacks.onDragEnd).not.toBeNull();
    dndCallbacks.onDragEnd!({
      active: { id: 'a' } as DragEndEvent['active'],
      over: { id: 'b' } as DragEndEvent['over'],
    } as DragEndEvent);

    await waitFor(() =>
      expect(api.putDashboardLayout).toHaveBeenCalledWith({
        provider_order: ['b', 'a'],
        card_orders: {},
      }),
    );
  });

  it('does not call putDashboardLayout when drag ends on the same card (#286)', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await screen.findByText('Claude');

    dndCallbacks.onDragEnd!({
      active: { id: 'claude' } as DragEndEvent['active'],
      over: { id: 'claude' } as DragEndEvent['over'],
    } as DragEndEvent);

    expect(api.putDashboardLayout).not.toHaveBeenCalled();
  });

  it('does not render the search input when there are ≤5 providers (#286)', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        provider({ provider_id: 'p1', name: 'Alpha' }),
        provider({ provider_id: 'p2', name: 'Bravo' }),
      ],
    });
    vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: [], card_orders: {} });
    renderV2(<ProvidersSection />);

    await screen.findByText('Alpha');
    expect(screen.queryByLabelText(/search providers/i)).not.toBeInTheDocument();
  });
});
