import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TokenHealthEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { TokensSection } from './TokensSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

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
  beforeEach(() => vi.clearAllMocks());

  it('shows skeletons while loading', () => {
    vi.mocked(api.fetchTokenHealth).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<TokensSection />);
    expect(container.querySelectorAll('[class*="animate-pulse"]').length).toBeGreaterThan(0);
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
});
