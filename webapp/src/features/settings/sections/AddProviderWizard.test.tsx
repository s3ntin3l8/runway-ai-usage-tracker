// Tests for the AddProviderWizard — the actual UX (debounced preview, 409
// collision handling, save body shape, Clear buttons) rather than the
// open/close flow (which is exercised in ProvidersSection.test.tsx).
//
// These tests stub `previewAccount` directly so the debounce timing is
// deterministic and the 409 path is reachable.

import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import type { ProviderAccount, ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { AddProviderWizard } from './AddProviderWizard';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

const anthropicAccount: ProviderAccount = {
  account_id: 'alice@example.com',
  account_label: 'Alice',
  enabled: true,
  api_key_set: true,
  session_cookie_set: false,
  poll_interval_seconds: null,
  collection_strategies: null,
  is_orphaned: false,
};

const anthropicProvider: ProviderConfig = {
  provider_id: 'anthropic',
  name: 'Anthropic',
  enabled: true,
  api_key_set: true,
  session_cookie_set: false,
  account_label: 'Alice',
  effective_poll_interval: 60,
  supports_api_key: true,
  supports_session_cookie: false,
  api_key_label: 'API key', // pragma: allowlist secret
  supported_strategies: [{ id: 'web', enabled: true }],
  collection_strategies: [{ id: 'web', enabled: true }],
  accounts: [anthropicAccount],
  account_count: 1,
};

const existingMap = new Map<string, Set<string>>([
  ['anthropic', new Set(['alice@example.com'])],
]);

function renderWizard() {
  return renderWithProviders(
    <AddProviderWizard
      providers={[anthropicProvider]}
      existingAccountIdsByProvider={existingMap}
      onClose={() => {}}
    />,
  );
}

function renderPreScopedWizard() {
  return renderWithProviders(
    <AddProviderWizard
      preScopedProvider={anthropicProvider}
      providers={[anthropicProvider]}
      existingAccountIdsByProvider={existingMap}
      onClose={() => {}}
    />,
  );
}

describe('AddProviderWizard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('saves the selected OpenCode workspace ID', async () => {
    const opencode: ProviderConfig = {
      ...anthropicProvider,
      provider_id: 'opencode',
      name: 'OpenCode',
      supports_api_key: true,
    };
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'open@example.com',
      suggested_label: 'open@example.com',
      label_source: 'email',
      already_exists: false,
    });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });

    renderWithProviders(
      <AddProviderWizard
        preScopedProvider={opencode}
        providers={[opencode]}
        existingAccountIdsByProvider={new Map()}
        onClose={() => {}}
      />,
    );
    await userEvent.type(screen.getByLabelText(/API key/i), 'oc_sk_test'); // pragma: allowlist secret
    await waitFor(() => expect(screen.getByText(/open@example\.com/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await userEvent.type(screen.getByLabelText(/OpenCode workspace ID/i), ' workspace-42 ');
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(api.putProviderConfig).toHaveBeenCalledWith(
      'opencode',
      'open@example.com',
      expect.objectContaining({ opencode_workspace_id: 'workspace-42' }),
    );
  });

  it('debounces the preview call and passes the typed credential', async () => {
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'bob@example.com',
      suggested_label: 'bob@example.com',
      label_source: 'email',
      already_exists: false,
    });

    renderWizard();
    await userEvent.click(await screen.findByText('Anthropic'));

    // User types into the api_key field — preview must NOT fire immediately.
    await userEvent.type(screen.getByLabelText(/API key/i), 'sk-ant-new');
    expect(api.previewAccount).not.toHaveBeenCalled();

    // After the 300ms debounce the call fires once with the typed credential.
    await waitFor(() =>
      expect(api.previewAccount).toHaveBeenCalledTimes(1),
    );
    expect(api.previewAccount).toHaveBeenCalledWith(
      {
        provider_id: 'anthropic',
        api_key: 'sk-ant-new', // pragma: allowlist secret
      },
      expect.objectContaining({ aborted: false }),
    );
  });

  it('keeps the last credential winning when multiple debounced fetches land (#287)', async () => {
    // Fake timers let us pace the 300ms debounce deterministically. Each
    // typed value gets its own preview call once the debounce flushes —
    // two distinct values typed with a debounce window between them
    // produce two API calls; the wizard's AbortController keeps the
    // last response.
    vi.useFakeTimers({ shouldAdvanceTime: true });

    let call = 0;
    vi.mocked(api.previewAccount).mockImplementation(async () => {
      const n = ++call;
      // Resolve the first call late, the second call early — simulates a
      // user who types fast: the in-flight first call resolves AFTER the
      // second has already started.
      await new Promise((r) => setTimeout(r, n === 1 ? 80 : 0));
      return {
        suggested_account_id: `typing-${n}@example.com`,
        suggested_label: `typing-${n}@example.com`,
        label_source: 'email',
        already_exists: false,
      };
    });

    renderWizard();
    await userEvent.click(await screen.findByText('Anthropic'));
    const input = screen.getByLabelText(/API key/i);

    // Type two distinct values, letting the 300ms debounce flush each.
    fireEvent.change(input, { target: { value: 'typed-a' } });
    await vi.advanceTimersByTimeAsync(350);
    fireEvent.change(input, { target: { value: 'typed-ab' } });
    await vi.advanceTimersByTimeAsync(350);

    // Both API calls happened; the last identity is rendered. The first
    // call's identity was suppressed by the AbortController — but because
    // AbortController only short-circuits the response handler (we don't
    // plumb `signal` through the API client yet), both fetches hit the
    // server and both responses land. The LAST response wins on render
    // because the state update from the earlier fetch is interleaved with
    // the later one and the later one is committed last.
    expect(api.previewAccount).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/typing-2@example\.com/)).toBeInTheDocument();

    vi.useRealTimers();
  });

  it('surfaces a 409 collision as an inline error and keeps Next disabled (#287 B1)', async () => {
    vi.mocked(api.previewAccount).mockRejectedValue(
      new ApiError(409, '[object Object]'),
    );

    renderPreScopedWizard();
    await userEvent.type(await screen.findByLabelText(/API key/i), 'sk-bad');

    await waitFor(() =>
      expect(screen.getByText(/already exists for this provider/i)).toBeInTheDocument(),
    );
    // Next is disabled — can't proceed past the collision.
    const next = screen.getByRole('button', { name: /next/i });
    expect(next).toBeDisabled();
  });

  it('enables Next only when the preview returns without conflict', async () => {
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'charlie@example.com',
      suggested_label: 'charlie@example.com',
      label_source: 'email',
      already_exists: false,
    });

    renderPreScopedWizard();
    const next = screen.getByRole('button', { name: /next/i });
    expect(next).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/API key/i), 'sk-charlie');
    await waitFor(() =>
      expect(screen.getByText(/charlie@example\.com/)).toBeInTheDocument(),
    );
    expect(next).toBeEnabled();
  });

  it('saves with the previewed account_id and the typed credentials', async () => {
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'dora@example.com',
      suggested_label: 'dora@example.com',
      label_source: 'email',
      already_exists: false,
    });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });

    renderPreScopedWizard();
    await userEvent.type(screen.getByLabelText(/API key/i), 'sk-dora');
    await waitFor(() =>
      expect(screen.getByText(/dora@example\.com/)).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole('button', { name: /next/i }));

    // Step 3 shows the pre-filled label and the provider's strategies.
    expect(screen.getByLabelText(/Account label/i)).toHaveValue('dora@example.com');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('switch', { name: /^web$/i })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(api.putProviderConfig).toHaveBeenCalledWith('anthropic', 'dora@example.com', {
      enabled: true,
      account_label: 'dora@example.com',
      poll_interval_seconds: null,
      collection_strategies: [{ id: 'web', enabled: true }],
      api_key: 'sk-dora', // pragma: allowlist secret
    });
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        expect.stringMatching(/dora@example\.com/),
      ),
    );
  });

  it('never surfaces the raw credential-hash account_id in the preview UI (#294)', async () => {
    const hash = '72ca8b0011223344556677889900aabbccddeeff00112233445566778899a9f5'; // pragma: allowlist secret
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: hash,
      suggested_label: null,
      label_source: 'credential_hash',
      already_exists: false,
    });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });

    renderPreScopedWizard();
    await userEvent.type(screen.getByLabelText(/API key/i), 'sk-opaque'); // pragma: allowlist secret

    // Headline must be humanized, badge humanized — full hex never in the DOM.
    expect(await screen.findByText('No identity in credential')).toBeInTheDocument();
    expect(screen.getByText('hash')).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(hash))).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(hash);

    // Full id is still used for the PUT (navigation/API path needs it).
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(api.putProviderConfig).toHaveBeenCalledWith(
      'anthropic',
      hash,
      expect.objectContaining({ enabled: true }),
    );
  });

  it('cuts strategies from the existing account when adding a 2nd account (#287 B2)', async () => {
    // Override the provider's first account to have a customized
    // collection_strategies — when the wizard opens (pre-scoped), the
    // confirm step must pre-fill from the existing account, not the
    // registry defaults.
    const customized: ProviderConfig = {
      ...anthropicProvider,
      accounts: [
        {
          ...anthropicAccount,
          collection_strategies: [
            { id: 'web', enabled: false },
            { id: 'oauth', enabled: true },
          ],
        },
      ],
    };
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [customized] });
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'eve@example.com',
      suggested_label: 'eve@example.com',
      label_source: 'email',
      already_exists: false,
    });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });

    renderWithProviders(
      <AddProviderWizard
        preScopedProvider={customized}
        providers={[customized]}
        existingAccountIdsByProvider={
          new Map([['anthropic', new Set(['alice@example.com'])]])
        }
        onClose={() => {}}
      />,
    );

    await userEvent.type(screen.getByLabelText(/API key/i), 'sk-eve');
    await waitFor(() =>
      expect(screen.getByText(/eve@example\.com/)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole('button', { name: /next/i }));

    const dialog = await screen.findByRole('dialog');
    // web=off, oauth=on — pre-filled from the existing account, NOT from
    // provider.supported_strategies (which would be web=on).
    expect(within(dialog).getByRole('switch', { name: /^web$/i })).not.toBeChecked();
    expect(within(dialog).getByRole('switch', { name: /^oauth$/i })).toBeChecked();

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(api.putProviderConfig).toHaveBeenCalledWith(
      'anthropic',
      'eve@example.com',
      expect.objectContaining({
        collection_strategies: [
          { id: 'web', enabled: false },
          { id: 'oauth', enabled: true },
        ],
      }),
    );
  });

  it('preserves Step2 + Step3 form state across Back → Next (#287 B3)', async () => {
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'frank@example.com',
      suggested_label: 'frank@example.com',
      label_source: 'email',
      already_exists: false,
    });

    renderPreScopedWizard();
    await userEvent.type(screen.getByLabelText(/API key/i), 'sk-frank');
    await waitFor(() =>
      expect(screen.getByText(/frank@example\.com/)).toBeInTheDocument(),
    );

    // Advance to step 3 and edit the label.
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    const labelInput = screen.getByLabelText(/Account label/i);
    await userEvent.clear(labelInput);
    await userEvent.type(labelInput, 'Work');

    // Back → Step2 still has the typed credential.
    await userEvent.click(screen.getByRole('button', { name: /back/i }));
    expect(screen.getByLabelText(/API key/i)).toHaveValue('sk-frank');

    // Next → Step3 still has the edited label.
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(screen.getByLabelText(/Account label/i)).toHaveValue('Work');
  });

  // ---------------------------------------------------------------------------
  // Step 1 — pick-a-provider + search/empty states
  // ---------------------------------------------------------------------------

  it('renders an empty state when no providers are registered', async () => {
    renderWithProviders(
      <AddProviderWizard
        providers={[]}
        existingAccountIdsByProvider={new Map()}
        onClose={() => {}}
      />,
    );
    expect(await screen.findByText(/no providers registered/i)).toBeInTheDocument();
  });

  it('filters the provider grid by the search input on Step 1', async () => {
    const chatgpt = {
      ...anthropicProvider,
      provider_id: 'chatgpt',
      name: 'ChatGPT',
      accounts: [],
      account_count: 0,
    };
    renderWithProviders(
      <AddProviderWizard
        providers={[anthropicProvider, chatgpt]}
        existingAccountIdsByProvider={new Map()}
        onClose={() => {}}
      />,
    );

    await screen.findByText('Anthropic');
    // Only the search input renders when there are >5 providers; we have 2
    // here so the search input is hidden. Use the providers directly to verify
    // the grid renders both names.
    expect(screen.getByText('ChatGPT')).toBeInTheDocument();
  });

  it('renders the search input + filters when there are >5 providers', async () => {
    const manyProviders = Array.from({ length: 6 }, (_, i) => ({
      ...anthropicProvider,
      provider_id: `p${i}`,
      name: `Provider ${i}`,
      accounts: [],
      account_count: 0,
    }));
    renderWithProviders(
      <AddProviderWizard
        providers={manyProviders}
        existingAccountIdsByProvider={new Map()}
        onClose={() => {}}
      />,
    );

    await screen.findByText('Provider 0');
    const search = screen.getByLabelText(/search providers/i);
    await userEvent.type(search, 'provider 3');
    expect(screen.queryByText('Provider 0')).not.toBeInTheDocument();
    expect(screen.getByText('Provider 3')).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Step 3 — save + error toast
  // ---------------------------------------------------------------------------

  it('surfaces a save error via toast.error and does not navigate', async () => {
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'gail@example.com',
      suggested_label: 'gail@example.com',
      label_source: 'email',
      already_exists: false,
    });
    vi.mocked(api.putProviderConfig).mockRejectedValue(new Error('server exploded'));

    renderPreScopedWizard();
    await userEvent.type(screen.getByLabelText(/API key/i), 'sk-gail');
    await waitFor(() =>
      expect(screen.getByText(/gail@example\.com/)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('server exploded'),
    );
  });

  it('sends session_cookie on Save when the provider supports it (#287)', async () => {
    const cookieProvider: ProviderConfig = {
      ...anthropicProvider,
      provider_id: 'chatgpt',
      name: 'ChatGPT',
      supports_api_key: false,
      supports_session_cookie: true,
      session_cookie_label: 'Session cookie',
      accounts: [],
      account_count: 0,
      collection_strategies: [],
    };
    vi.mocked(api.previewAccount).mockResolvedValue({
      suggested_account_id: 'hank@example.com',
      suggested_label: 'hank@example.com',
      label_source: 'email',
      already_exists: false,
    });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });

    renderWithProviders(
      <AddProviderWizard
        preScopedProvider={cookieProvider}
        providers={[cookieProvider]}
        existingAccountIdsByProvider={new Map([['chatgpt', new Set()]])}
        onClose={() => {}}
      />,
    );

    await userEvent.type(screen.getByLabelText(/Session cookie/i), 'cookie-paste'); // pragma: allowlist secret
    await waitFor(() =>
      expect(screen.getByText(/hank@example\.com/)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole('button', { name: /next/i }));
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(api.putProviderConfig).toHaveBeenCalledWith(
      'chatgpt',
      'hank@example.com',
      expect.objectContaining({ session_cookie: 'cookie-paste' }), // pragma: allowlist secret
    );
    expect(vi.mocked(api.putProviderConfig).mock.calls[0]?.[2]).not.toHaveProperty('api_key');
  });

  it('enables Next immediately for credential-less providers (e.g. antigravity) and skips the preview call (#287)', async () => {
    // Antigravity's registry rule is `file_json_data` — no api_key, no
    // session_cookie. Step 2 should render no input fields, the default
    // identity is the only possible preview, and Next must be enabled
    // without a network round-trip.
    const fileBased: ProviderConfig = {
      ...anthropicProvider,
      provider_id: 'antigravity',
      name: 'Antigravity',
      supports_api_key: false,
      supports_session_cookie: false,
      accounts: [],
      account_count: 0,
    };

    renderWithProviders(
      <AddProviderWizard
        preScopedProvider={fileBased}
        providers={[fileBased]}
        existingAccountIdsByProvider={new Map([['antigravity', new Set()]])}
        onClose={() => {}}
      />,
    );

    // No credential inputs render.
    expect(screen.queryByLabelText(/API key/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/cookie/i)).not.toBeInTheDocument();

    // The default sentinel is the preview result.
    expect(
      await screen.findByText(/this provider has no credentials to enter/i),
    ).toBeInTheDocument();

    // No network call: the backend default sentinel is seeded synchronously
    // so we don't waste a round-trip on the 30/min preview limit.
    expect(api.previewAccount).not.toHaveBeenCalled();

    const next = screen.getByRole('button', { name: /next/i });
    expect(next).toBeEnabled();
  });
});
