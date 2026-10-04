// Silent-listener dialog unit tests (PR #288).
//
// The fleet page-level tests cover the entry-point wiring (banner + per-card
// badge → dialog opens). This file drives the dialog's own rendering
// contract: the Radix Select trigger, the provider-scoped dropdown, the
// Tag button's payload, and the empty-state fallback when no provider
// rows exist.

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { UntaggedCredentialsDialog } from './UntaggedCredentialsDialog';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const entry = {
  sidecar_id: 'laptop',
  provider_id: 'anthropic',
  credential_origin: 'provider:anthropic',
};

// Server response shape (PR #288 multi-account): each ProviderConfig
// has an ``accounts`` array — one entry per ``provider_configs`` row.
// The dialog iterates ``accounts``, not the provider-level fields.
const anthropicRow = {
  provider_id: 'anthropic',
  name: 'Anthropic',
  accounts: [
    {
      account_id: 'alice@example.com',
      account_label: 'Alice',
      enabled: true,
      api_key_set: false,
      session_cookie_set: false,
      poll_interval_seconds: null,
      collection_strategies: null,
      is_orphaned: false,
    },
  ],
  account_count: 1,
};
const chatgptRow = {
  provider_id: 'chatgpt',
  name: 'ChatGPT',
  accounts: [
    {
      account_id: 'default',
      account_label: 'Default',
      enabled: true,
      api_key_set: false,
      session_cookie_set: false,
      poll_interval_seconds: null,
      collection_strategies: null,
      is_orphaned: false,
    },
  ],
  account_count: 1,
};

describe('UntaggedCredentialsDialog', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders an empty state when there are no pending entries', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({ items: [], counts_by_sidecar: {} });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );
    expect(
      await screen.findByText(/no credentials waiting for a tag/i),
    ).toBeInTheDocument();
  });

  it('shows only provider-scoped provider_config rows', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [anthropicRow, chatgptRow],
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    // Wait for the row to render — the query starts on `open=true` and the
    // first render shows the empty state until the data lands.
    await within(dialog).findByText(/provider:anthropic/);
    // Open the Radix Select popover.
    const trigger = within(dialog).getByRole('combobox');
    await user.click(trigger);
    // Anthropic row renders; chatgpt row is filtered out because the
    // dropdown is provider-scoped to the entry's provider_id.
    await screen.findByText('Alice · alice@example.com');
    expect(screen.queryByText('Default')).not.toBeInTheDocument();
  });

  it('shows an unresolved source quota preview without presenting it as account history', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        {
          ...entry,
          quota_preview_observed_at: '2026-09-29T10:00:00Z',
          quota_preview: [
            { service_name: 'Antigravity', remaining: 7, unit: 'requests', pct_used: 30 },
            { remaining: 4 },
            { service_name: 'Gemini', window_type: 'weekly' },
          ],
        },
      ],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [anthropicRow] });
    renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={() => {}} />);

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('Live quota from this credential')).toBeInTheDocument();
    expect(within(dialog).getByText(/Observed/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Antigravity · 7 requests · 30% used/)).toBeInTheDocument();
    expect(within(dialog).getByText(/not added to account history until assigned/i)).toBeInTheDocument();
  });

  it('shows when a persisted quota preview has expired', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        {
          ...entry,
          quota_preview: [],
          quota_preview_stale: true,
          quota_preview_observed_at: '2026-09-29T17:00:00+00:00',
        },
      ],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [anthropicRow] });
    renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={() => {}} />);

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByRole('status')).toHaveTextContent(/preview expired/i);
    expect(within(dialog).getByRole('status')).toHaveTextContent(/last observed/i);
  });

  it('shows an expired preview without a last-observed timestamp when unavailable', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [{ ...entry, quota_preview: [], quota_preview_stale: true }],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [anthropicRow] });
    renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={() => {}} />);

    const dialog = await screen.findByRole('dialog');
    const status = await within(dialog).findByRole('status');
    expect(status).toHaveTextContent(/preview expired/i);
    expect(status).not.toHaveTextContent(/last observed/i);
  });

  it('shows the "Add one in Provider Settings" fallback when no provider row exists', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    const user = userEvent.setup();
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    // The "Add one in Provider Settings" anchor only renders when no
    // provider_config row matches the entry's provider_id. Its presence
    // is the contract.
    const link = await within(dialog).findByRole('link', {
      name: /add one in provider settings/i,
    });
    expect(link.closest('p')?.textContent).toMatch(/no .*anthropic.* row configured/i);
    // No Tag button when there are no provider rows to pick from.
    expect(within(dialog).queryByRole('button', { name: /^tag$/i })).not.toBeInTheDocument();
    await user.click(link); // exercise the link without error
  });

  it('names the machine and links to Provider Settings without a full page reload', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: 'laptop', hostname: 'laptop', custom_name: 'My Laptop' }] as never,
    });
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={vi.fn()} />);

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('My Laptop')).toBeInTheDocument();
    // A client-side route: the old `<a href="/settings/providers#id">` reloaded the SPA
    // and the hash was never read.
    expect(
      within(dialog).getByRole('link', { name: /add one in provider settings/i }),
    ).toHaveAttribute('href', '/settings/providers');
  });

  it('sends tagCredential with the operator-selected account_id', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [anthropicRow],
    });
    vi.mocked(api.tagCredential).mockResolvedValue({ status: 'ok' });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onClose = vi.fn();
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={onClose} />,
    );

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    // Open the Radix Select popover and pick the anthropic row.
    const trigger = within(dialog).getByRole('combobox');
    await user.click(trigger);
    await user.click(await screen.findByText('Alice · alice@example.com'));
    // Click the Tag button — the dialog's onSave callback then fires.
    await user.click(within(dialog).getByRole('button', { name: /^tag$/i }));

    await waitFor(() =>
      expect(api.tagCredential).toHaveBeenCalledWith({
        sidecar_id: 'laptop',
        provider_id: 'anthropic',
        credential_origin: 'provider:anthropic',
        account_id: 'alice@example.com',
        // #319: default scope is "This machine" (sidecar-scoped).
        scope: 'sidecar',
      }),
    );
    // Several credentials may be waiting, so a successful save toasts and stays open
    // on the next one rather than forcing the operator to reopen the dialog each time.
    expect(toast.success).toHaveBeenCalledWith('Credential tagged');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes after tagging when resolving a single entry', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [anthropicRow] });
    vi.mocked(api.tagCredential).mockResolvedValue({ status: 'ok' });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onClose = vi.fn();
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={onClose} singleEntry={entry} />,
    );

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    await user.click(within(dialog).getByRole('combobox'));
    await user.click(await screen.findByText('Alice · alice@example.com'));
    await user.click(within(dialog).getByRole('button', { name: /^tag$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('refreshes the provider configs under the shared query key after tagging', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [anthropicRow] });
    vi.mocked(api.tagCredential).mockResolvedValue({ status: 'ok' });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={vi.fn()} />);

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    // Previously fetched under ['system','provider_configs'] — a different key from
    // Providers/Webhooks, so tagging never refreshed them and the dialog double-fetched.
    await waitFor(() => expect(api.fetchProviderConfigs).toHaveBeenCalledTimes(1));
    await user.click(within(dialog).getByRole('combobox'));
    await user.click(await screen.findByText('Alice · alice@example.com'));
    await user.click(within(dialog).getByRole('button', { name: /^tag$/i }));

    // Invalidating ['system','provider-configs'] refetches the (active) dialog query.
    await waitFor(() => expect(api.fetchProviderConfigs).toHaveBeenCalledTimes(2));
  });

  it('sends scope=deployment when "All machines" is selected (#319)', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [anthropicRow],
    });
    vi.mocked(api.tagCredential).mockResolvedValue({ status: 'ok' });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    // Flip the dialog-level scope switch to "All machines".
    await user.click(within(dialog).getByRole('radio', { name: /all machines/i }));
    const trigger = within(dialog).getByRole('combobox');
    await user.click(trigger);
    await user.click(await screen.findByText('Alice · alice@example.com'));
    await user.click(within(dialog).getByRole('button', { name: /^tag$/i }));

    await waitFor(() =>
      expect(api.tagCredential).toHaveBeenCalledWith(
        expect.objectContaining({ scope: 'deployment' }),
      ),
    );
  });

  it('offers no "All machines" scope for a browser-cookie credential', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [{ ...entry, credential_origin: 'cookie:anthropic/session' }],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [anthropicRow] });
    vi.mocked(api.tagCredential).mockResolvedValue({ status: 'ok' });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={() => {}} />);

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/cookie:anthropic\/session/);

    expect(within(dialog).getByRole('radio', { name: /all machines/i })).toBeDisabled();
    expect(within(dialog).getByText(/belong to one machine/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('combobox'));
    await user.click(await screen.findByText('Alice · alice@example.com'));
    await user.click(within(dialog).getByRole('button', { name: /^tag$/i }));
    await waitFor(() =>
      expect(api.tagCredential).toHaveBeenCalledWith(expect.objectContaining({ scope: 'sidecar' })),
    );
  });

  it('defaults the scope switch to "This machine" and resets on reopen', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [anthropicRow],
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const { rerender } = renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    const thisMachine = within(dialog).getByRole('radio', { name: /this machine/i });
    expect(thisMachine).toHaveAttribute('aria-checked', 'true');

    await user.click(within(dialog).getByRole('radio', { name: /all machines/i }));
    expect(
      within(dialog).getByRole('radio', { name: /all machines/i }),
    ).toHaveAttribute('aria-checked', 'true');

    // Close and reopen: scope resets to the "This machine" default.
    rerender(<UntaggedCredentialsDialog open={false} onClose={() => {}} />);
    rerender(<UntaggedCredentialsDialog open={true} onClose={() => {}} />);
    const reopened = await screen.findByRole('dialog');
    expect(
      within(reopened).getByRole('radio', { name: /this machine/i }),
    ).toHaveAttribute('aria-checked', 'true');
  });

  describe('scope is chosen per row', () => {
    const two = [
      { sidecar_id: 'laptop', provider_id: 'anthropic', credential_origin: 'path:/home/u/a.json' },
      { sidecar_id: 'desktop', provider_id: 'anthropic', credential_origin: 'path:/home/u/b.json' },
    ];

    async function openTwoRows() {
      vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
        items: two,
        counts_by_sidecar: { laptop: 1, desktop: 1 },
      });
      vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [anthropicRow] });
      vi.mocked(api.tagCredential).mockResolvedValue({ status: 'ok' });
      const user = userEvent.setup({ pointerEventsCheck: 0 });
      renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={() => {}} />);
      const dialog = await screen.findByRole('dialog');
      await within(dialog).findByText(/a\.json/);
      return { user, dialog };
    }

    const scopeGroup = (dialog: HTMLElement, file: string) =>
      within(dialog).getByRole('radiogroup', { name: new RegExp(file) });

    it('choosing All machines on one row does not change another row', async () => {
      const { user, dialog } = await openTwoRows();

      await user.click(within(scopeGroup(dialog, 'a.json')).getByRole('radio', { name: /all machines/i }));

      expect(
        within(scopeGroup(dialog, 'a.json')).getByRole('radio', { name: /all machines/i }),
      ).toHaveAttribute('aria-checked', 'true');
      expect(
        within(scopeGroup(dialog, 'b.json')).getByRole('radio', { name: /this machine/i }),
      ).toHaveAttribute('aria-checked', 'true');
    });

    it('moving to another row drops the previous row\'s all-machines choice', async () => {
      const { user, dialog } = await openTwoRows();

      await user.click(within(scopeGroup(dialog, 'a.json')).getByRole('radio', { name: /all machines/i }));
      await user.click(within(scopeGroup(dialog, 'b.json')).getByRole('radio', { name: /this machine/i }));
      await user.click(within(scopeGroup(dialog, 'a.json')).getByRole('radio', { name: /this machine/i }));

      expect(
        within(scopeGroup(dialog, 'a.json')).getByRole('radio', { name: /this machine/i }),
      ).toHaveAttribute('aria-checked', 'true');
    });

    it('warns that a broad rule on a plain path is not tied to the credential', async () => {
      const { user, dialog } = await openTwoRows();
      expect(within(dialog).queryByText(/isn't tied to the credential itself/i)).toBeNull();

      await user.click(within(scopeGroup(dialog, 'a.json')).getByRole('radio', { name: /all machines/i }));

      expect(within(dialog).getByText(/isn't tied to the credential itself/i)).toBeInTheDocument();
    });

    it('sends deployment only for the row that chose it, and resets afterwards', async () => {
      const { user, dialog } = await openTwoRows();

      await user.click(within(scopeGroup(dialog, 'a.json')).getByRole('radio', { name: /all machines/i }));
      // Rows render in list order, so row A is the first.
      await user.click(within(dialog).getAllByRole('combobox')[0]);
      await user.click(await screen.findByText('Alice · alice@example.com'));
      await user.click(within(dialog).getAllByRole('button', { name: /^tag$/i })[0]);

      await waitFor(() =>
        expect(api.tagCredential).toHaveBeenCalledWith(
          expect.objectContaining({ credential_origin: 'path:/home/u/a.json', scope: 'deployment' }),
        ),
      );
      // After a save the staged selection (account and scope) is cleared.
      await waitFor(() =>
        expect(
          within(scopeGroup(dialog, 'b.json')).getByRole('radio', { name: /this machine/i }),
        ).toHaveAttribute('aria-checked', 'true'),
      );
    });
  });

  it('toasts an error and keeps the dialog open when the save fails', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [anthropicRow],
    });
    vi.mocked(api.tagCredential).mockRejectedValue(new Error('tagging failed'));
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    const trigger = within(dialog).getByRole('combobox');
    await user.click(trigger);
    await user.click(await screen.findByText('Alice · alice@example.com'));
    await user.click(within(dialog).getByRole('button', { name: /^tag$/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('tagging failed'));
    // Dialog stays open on failure (operator can retry).
    expect(within(dialog).getByRole('button', { name: /^tag$/i })).toBeInTheDocument();
  });

  it('renders in banner mode (singleEntry=undefined) showing every pending entry', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [
        entry,
        {
          sidecar_id: 'desktop',
          provider_id: 'chatgpt',
          credential_origin: 'provider:chatgpt',
        },
      ],
      counts_by_sidecar: { laptop: 1, desktop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    // Wait for both rows to render before asserting.
    await within(dialog).findByText(/provider:anthropic/);
    expect(within(dialog).getByText(/provider:chatgpt/)).toBeInTheDocument();
  });

  it('shows the discovered Claude login for an unassigned credential', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [{ ...entry, provider_id: 'anthropic', claimed_account_id: 'alice@example.com' }],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
    renderWithProviders(<UntaggedCredentialsDialog open={true} onClose={() => {}} />);

    expect(await screen.findByText('alice@example.com')).toBeInTheDocument();
    expect(screen.getByText(/Discovered login:/)).toBeInTheDocument();
  });

  it('filters disabled accounts from the dropdown and surfaces an Enable hint', async () => {
    // PR #290 round-2 review (Hermes body suggestion #4): tagging to
    // a disabled row stores a hint the server won't collect (the
    // /fleet/config ``accounts`` view only ships enabled rows).
    // Filter disabled rows out and show an Enable hint instead.
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        {
          provider_id: 'anthropic',
          name: 'Anthropic',
          accounts: [
            {
              account_id: 'alice@example.com',
              account_label: 'Alice',
              enabled: true,
              api_key_set: false,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false,
            },
            {
              account_id: 'bob@example.com',
              account_label: 'Bob',
              enabled: false,
              api_key_set: false,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false,
            },
          ],
          account_count: 2,
        },
      ],
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    const trigger = within(dialog).getByRole('combobox');
    await user.click(trigger);
    // Only the enabled row appears; Bob is filtered out.
    await screen.findByText('Alice · alice@example.com');
    expect(screen.queryByText('Bob · bob@example.com')).not.toBeInTheDocument();
  });

  it('shows the "all rows disabled" message with an Enable link', async () => {
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
      items: [entry],
      counts_by_sidecar: { laptop: 1 },
    });
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        {
          provider_id: 'anthropic',
          name: 'Anthropic',
          accounts: [
            {
              account_id: 'bob@example.com',
              account_label: 'Bob',
              enabled: false,
              api_key_set: false,
              session_cookie_set: false,
              poll_interval_seconds: null,
              collection_strategies: null,
              is_orphaned: false,
            },
          ],
          account_count: 1,
        },
      ],
    });
    renderWithProviders(
      <UntaggedCredentialsDialog open={true} onClose={() => {}} />,
    );

    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/provider:anthropic/);
    // No Tag button when all rows are disabled.
    expect(within(dialog).queryByRole('button', { name: /^tag$/i })).not.toBeInTheDocument();
    // The Enable-in-Provider-Settings link is the precondition.
    const link = await within(dialog).findByRole('link', {
      name: /enable in provider settings/i,
    });
    expect(link.closest('p')?.textContent).toMatch(/all .*anthropic.* rows? configured .* disabled/i);
  });
});

describe('stageToBody', () => {
  const staged = {
    sidecar_id: 'laptop',
    provider_id: 'anthropic',
    credential_origin: 'provider:anthropic',
    account_id: 'alice@example.com',
    all_machines: false,
  };

  it('builds the body from the account staged for this row', async () => {
    const { stageToBody } = await import('./UntaggedCredentialsDialog');
    expect(stageToBody(entry, staged)).toEqual({
      ...entry,
      account_id: 'alice@example.com',
      scope: 'sidecar',
    });
    expect(stageToBody(entry, { ...staged, all_machines: true })?.scope).toBe('deployment');
  });

  it('never stages an all-machines tag for a cookie or keychain origin', async () => {
    const { stageToBody, isMachineBoundOrigin } = await import('./UntaggedCredentialsDialog');
    for (const origin of ['cookie:anthropic/session', 'keychain:Claude Code-credentials']) {
      const e = { ...entry, credential_origin: origin };
      const state = { ...staged, credential_origin: origin, all_machines: true };
      expect(isMachineBoundOrigin(origin)).toBe(true);
      expect(stageToBody(e, state)?.scope).toBe('sidecar');
    }
    expect(isMachineBoundOrigin('path:/shared/auth.json')).toBe(false);
  });

  it("never saves another machine's staged pick onto this row", async () => {
    const { stageToBody } = await import('./UntaggedCredentialsDialog');
    const otherMachine = { ...entry, sidecar_id: 'desktop' };
    expect(stageToBody(otherMachine, staged)).toBeNull();
  });

  it('returns null instead of guessing when nothing is staged', async () => {
    const { stageToBody } = await import('./UntaggedCredentialsDialog');
    expect(stageToBody(entry, { ...staged, account_id: '' })).toBeNull();
  });
});
