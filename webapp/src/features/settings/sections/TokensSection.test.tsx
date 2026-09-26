import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TokenHealthEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { TokensSection } from './TokensSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// Render in table mode (md+ breakpoint) so existing row-based assertions hold.
// Card-layout behaviour is a separate visual concern tested at the CSS/snapshot level.
const media = vi.hoisted(() => ({ isMd: true }));
vi.mock('@/hooks/useMediaQuery', () => ({
  useMediaQuery: () => media.isMd,
  useIsDesktop: () => false,
}));

const token = (o: Partial<TokenHealthEntry> = {}): TokenHealthEntry => ({
  provider: 'claude',
  account_id: 'acc-1',
  account_label: 'Work',
  source: 'oauth',
  token_types: ['access', 'refresh'],
  status: 'valid',
  expires_at: '2026-07-01T00:00:00Z',
  can_refresh: true,
  ...o,
});

describe('TokensSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    media.isMd = true;
  });

  it('shows skeletons while loading', () => {
    vi.mocked(api.fetchTokenHealth).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<TokensSection />);
    expect(container.querySelectorAll('[class*="animate-shimmer"]').length).toBeGreaterThan(0);
  });

  it('renders the error state', async () => {
    vi.mocked(api.fetchTokenHealth).mockRejectedValue(new Error('nope'));
    renderWithProviders(<TokensSection />);
    expect(await screen.findByText(/token health unavailable/i)).toBeInTheDocument();
    expect(screen.getByText('nope')).toBeInTheDocument();
  });

  it('shows the empty state when there are no cached tokens', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [] });
    renderWithProviders(<TokensSection />);
    expect(await screen.findByText(/no cached credentials/i)).toBeInTheDocument();
  });

  it('renders a token row with its provider, label and status', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [token()] });
    renderWithProviders(<TokensSection />);
    expect(await screen.findByText('claude')).toBeInTheDocument();
    expect(screen.getByText('Work')).toBeInTheDocument();
    expect(screen.getByText('valid')).toBeInTheDocument();
  });

  it('refreshes a token via the refresh endpoint', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [token()] });
    vi.mocked(api.postTokenRefresh).mockResolvedValue(undefined as never);
    renderWithProviders(<TokensSection />);

    await userEvent.click(await screen.findByRole('button', { name: /refresh token/i }));
    expect(api.postTokenRefresh).toHaveBeenCalledWith('claude', 'acc-1');
  });

  it('removes a token from cache and hides refresh when not refreshable', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [token({ can_refresh: false })],
    });
    vi.mocked(api.deleteTokenHealth).mockResolvedValue(undefined as never);
    renderWithProviders(<TokensSection />);

    expect(screen.queryByRole('button', { name: /refresh token/i })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: /remove from cache/i }));
    expect(api.deleteTokenHealth).toHaveBeenCalledWith('claude', 'acc-1');
  });

  it('renders a TTL label when ttl_remaining_seconds is present', async () => {
    // 2700 s = 45m — should appear as "TTL: 45m"
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [token({ ttl_remaining_seconds: 2700 })],
    });
    renderWithProviders(<TokensSection />);
    expect(await screen.findByText(/TTL: 45m/)).toBeInTheDocument();
  });

  it('omits the TTL label when ttl_remaining_seconds is 0', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [token({ ttl_remaining_seconds: 0 })],
    });
    renderWithProviders(<TokensSection />);
    await screen.findByText('valid'); // wait for render
    expect(screen.queryByText(/TTL:/)).not.toBeInTheDocument();
  });

  it('shows a "redundant" badge and dims the row for redundant tokens', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [token({ status: 'expired', redundant: true, can_refresh: false })],
    });
    renderWithProviders(<TokensSection />);
    expect(await screen.findByText('redundant')).toBeInTheDocument();
    expect(screen.getByText('expired')).toBeInTheDocument();
  });

  it('does not show the "redundant" badge for non-redundant tokens', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [token({ status: 'expired', redundant: false, can_refresh: false })],
    });
    renderWithProviders(<TokensSection />);
    // Wait for the status badge to render.
    expect(await screen.findByText('expired')).toBeInTheDocument();
    expect(screen.queryByText('redundant')).not.toBeInTheDocument();
  });

  it('renders sidecar source name but hides generic "config" source', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ source_name: 'my-laptop' }),
        token({ source_name: 'config', account_id: 'acc-2' }),
      ],
    });
    renderWithProviders(<TokensSection />);
    // Wait for rows to render (multiple 'valid' badges expected).
    expect(await screen.findAllByText('valid')).toHaveLength(2);
    expect(screen.getByText('my-laptop')).toBeInTheDocument();
    // Only the sidecar account should render a sidecar-origin badge — the 'config'
    // account renders plain text, not a badge with that title.
    expect(screen.getAllByTitle(/originates from this sidecar/i)).toHaveLength(1);
  });

  // ── Sorting ─────────────────────────────────────────────────────────────

  it('sorts rows by provider when the Provider header is clicked', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ provider: 'openai', account_id: 'a1' }),
        token({ provider: 'claude', account_id: 'a2' }),
        token({ provider: 'gemini', account_id: 'a3' }),
      ],
    });
    renderWithProviders(<TokensSection />);

    // Wait for render.
    await screen.findByRole('button', { name: /^provider$/i });

    // First click → desc (default for non-validity columns).
    await userEvent.click(screen.getByRole('button', { name: /^provider$/i }));
    const rowsAfterDesc = screen.getAllByRole('row').slice(1); // skip header
    expect(within(rowsAfterDesc[0]).getByText('openai')).toBeInTheDocument();

    // Second click → asc.
    await userEvent.click(screen.getByRole('button', { name: /^provider$/i }));
    const rowsAfterAsc = screen.getAllByRole('row').slice(1);
    expect(within(rowsAfterAsc[0]).getByText('claude')).toBeInTheDocument();
  });

  it('defaults to validity-ascending so expired tokens appear first', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ account_id: 'a1', status: 'valid' }),
        token({ account_id: 'a2', status: 'expired', can_refresh: false }),
        token({ account_id: 'a3', status: 'expiring' }),
      ],
    });
    renderWithProviders(<TokensSection />);

    // Wait for rows.
    await screen.findByText('expired');
    const rows = screen.getAllByRole('row').slice(1);
    // First row should be 'expired' (severity 0 ascending).
    expect(within(rows[0]).getByText('expired')).toBeInTheDocument();
    // Last row should be 'valid' (severity 3).
    expect(within(rows[rows.length - 1]).getByText('valid')).toBeInTheDocument();
  });

  // ── Filtering ────────────────────────────────────────────────────────────

  it('filters rows by provider via the provider Select', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ provider: 'claude', account_id: 'a1', account_label: 'Claude account' }),
        token({ provider: 'openai', account_id: 'a2', account_label: 'OpenAI account' }),
      ],
    });
    renderWithProviders(<TokensSection />);
    await screen.findByText('Claude account');

    // Open the provider filter and pick 'openai'.
    await userEvent.click(screen.getByRole('combobox', { name: /filter by provider/i }));
    await userEvent.click(await screen.findByRole('option', { name: 'openai' }));

    expect(screen.queryByText('Claude account')).not.toBeInTheDocument();
    expect(screen.getByText('OpenAI account')).toBeInTheDocument();
  });

  it('filters rows by status via the status Select', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ account_id: 'a1', status: 'valid', account_label: 'Valid acc' }),
        token({ account_id: 'a2', status: 'expired', account_label: 'Expired acc', can_refresh: false }),
      ],
    });
    renderWithProviders(<TokensSection />);
    await screen.findByText('Valid acc');

    await userEvent.click(screen.getByRole('combobox', { name: /filter by status/i }));
    await userEvent.click(await screen.findByRole('option', { name: 'expired' }));

    expect(screen.queryByText('Valid acc')).not.toBeInTheDocument();
    expect(screen.getByText('Expired acc')).toBeInTheDocument();
  });

  it('filters rows by origin via the origin Select', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ account_id: 'a1', source_name: 'my-laptop', account_label: 'Laptop acc' }),
        token({ account_id: 'a2', source_name: 'config', account_label: 'Config acc' }),
      ],
    });
    renderWithProviders(<TokensSection />);
    await screen.findByText('Laptop acc');

    await userEvent.click(screen.getByRole('combobox', { name: /filter by origin/i }));
    await userEvent.click(await screen.findByRole('option', { name: 'my-laptop' }));

    expect(screen.queryByText('Config acc')).not.toBeInTheDocument();
    expect(screen.getByText('Laptop acc')).toBeInTheDocument();
  });

  it('shows "no credentials match" when filters exclude all rows', async () => {
    // Two tokens: claude/valid and openai/expired. Filtering by provider=claude
    // AND status=expired yields zero results (the 'expired' option exists in the
    // status dropdown because it's derived from the full token list before filtering).
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ provider: 'claude', account_id: 'a1', status: 'valid' }),
        token({ provider: 'openai', account_id: 'a2', status: 'expired', can_refresh: false }),
      ],
    });
    renderWithProviders(<TokensSection />);
    await screen.findByText('claude');

    // Narrow to claude only.
    await userEvent.click(screen.getByRole('combobox', { name: /filter by provider/i }));
    await userEvent.click(await screen.findByRole('option', { name: 'claude' }));

    // Then narrow to expired — no claude token has that status.
    await userEvent.click(screen.getByRole('combobox', { name: /filter by status/i }));
    await userEvent.click(await screen.findByRole('option', { name: 'expired' }));

    expect(screen.getByText(/no credentials match/i)).toBeInTheDocument();
  });
  it('renders an "invalid" status for a credential the provider rejected', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [token({ status: 'invalid', can_refresh: false, expires_at: null })],
    });
    renderWithProviders(<TokensSection />);
    expect(await screen.findByText('invalid')).toBeInTheDocument();
  });

  it('sorts invalid credentials ahead of valid ones', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({ provider: 'aaa', account_id: 'v', status: 'valid' }),
        token({ provider: 'zzz', account_id: 'i', status: 'invalid', can_refresh: false }),
      ],
    });
    renderWithProviders(<TokensSection />);
    await screen.findByText('aaa');
    const rows = screen.getAllByRole('row').slice(1);
    expect(within(rows[0]).getByText('zzz')).toBeInTheDocument();
  });

  it('hides the remove button for managed (config/server) credentials', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        token({
          provider: 'zai',
          account_id: 'server',
          account_label: null,
          source: 'server',
          source_name: 'server',
          removable: false,
          can_refresh: false,
          token_types: ['api_key'],
        }),
        token({
          provider: 'openrouter',
          account_id: 'config:default',
          account_label: null,
          source: 'config',
          source_name: 'config',
          removable: false,
          can_refresh: false,
          token_types: ['api_key'],
        }),
      ],
    });
    renderWithProviders(<TokensSection />);

    expect(await screen.findByText('Server environment')).toBeInTheDocument();
    expect(screen.getByText('Default account')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /remove from cache/i })).not.toBeInTheDocument();
    expect(screen.getAllByText('managed')).toHaveLength(2);
  });
  describe('mobile card layout (<md)', () => {
    beforeEach(() => {
      media.isMd = false;
    });

    it('renders a card with status, friendly identifier and sidecar origin', async () => {
      vi.mocked(api.fetchTokenHealth).mockResolvedValue({
        tokens: [
          token({
            account_id: 'config:default',
            account_label: null,
            source_name: 'laptop',
            ttl_remaining_seconds: 600,
          }),
        ],
      });
      renderWithProviders(<TokensSection />);

      expect(await screen.findByText('Default account')).toBeInTheDocument();
      expect(screen.getByText('valid')).toBeInTheDocument();
      expect(screen.getByText('laptop')).toBeInTheDocument();
      expect(screen.getByText(/TTL:/)).toBeInTheDocument();
      expect(screen.queryByRole('table')).not.toBeInTheDocument();
    });

    it('shows "managed" instead of the remove button for config/server credentials', async () => {
      vi.mocked(api.fetchTokenHealth).mockResolvedValue({
        tokens: [
          token({
            provider: 'zai',
            account_id: 'server',
            account_label: null,
            source_name: 'server',
            removable: false,
            can_refresh: false,
          }),
        ],
      });
      renderWithProviders(<TokensSection />);

      expect(await screen.findByText('Server environment')).toBeInTheDocument();
      expect(screen.getByText('managed')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /remove from cache/i })).not.toBeInTheDocument();
    });

    it('lets a sidecar credential be refreshed and removed from its card', async () => {
      vi.mocked(api.fetchTokenHealth).mockResolvedValue({
        tokens: [token({ status: 'invalid', removable: true })],
      });
      vi.mocked(api.postTokenRefresh).mockResolvedValue(undefined as never);
      vi.mocked(api.deleteTokenHealth).mockResolvedValue(undefined as never);
      renderWithProviders(<TokensSection />);

      expect(await screen.findByText('invalid')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: /refresh token/i }));
      expect(api.postTokenRefresh).toHaveBeenCalledWith('claude', 'acc-1');
      await userEvent.click(screen.getByRole('button', { name: /remove from cache/i }));
      expect(api.deleteTokenHealth).toHaveBeenCalledWith('claude', 'acc-1');
    });

    it('marks redundant credentials and prints an expiry line', async () => {
      vi.mocked(api.fetchTokenHealth).mockResolvedValue({
        tokens: [token({ status: 'expired', redundant: true, can_refresh: false })],
      });
      renderWithProviders(<TokensSection />);

      expect(await screen.findByText('redundant')).toBeInTheDocument();
      expect(screen.getByText(/expires/i)).toBeInTheDocument();
    });
  });
});
