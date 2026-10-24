import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { MergedSection } from './MergedSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

const mockNavigate = vi.fn();

vi.mock('react-router', async () => {
  const actual = await vi.importActual('react-router');
  return { ...actual, useNavigate: () => mockNavigate };
});

const merged = [
  {
    provider_id: 'openrouter',
    account_id: 'alice@example.com',
    destination_account_id: 'bob@example.com',
    merged_at: '2026-10-20T10:00:00+00:00',
  },
];

describe('MergedSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        {
          provider_id: 'openrouter',
          name: 'OpenRouter',
          accounts: [
            { account_id: 'bob@example.com', account_label: 'Bob', enabled: true },
          ],
        } as never,
      ],
    });
  });

  it('names the account that was merged away and where it went', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      fleet: [],
      generated_at: '2026-10-20T10:00:00+00:00',
      merged_accounts: merged,
    } as never);
    renderWithProviders(<MergedSection />);

    expect(await screen.findByText('Merged')).toBeInTheDocument();
    expect(screen.getByText('OpenRouter')).toBeInTheDocument();
    // The account that was merged away, and the account it became (its label
    // when it has one).
    expect(screen.getByText(/alice@example\.com/)).toBeInTheDocument();
    expect(screen.getByText(/Bob/)).toBeInTheDocument();
  });

  it('links to the destination with the account id encoded', async () => {
    // An account id with a space (or a "#") must survive the query string:
    // unencoded, the reader would decode it into another account.
    mockNavigate.mockClear();
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      fleet: [],
      generated_at: '2026-10-20T10:00:00+00:00',
      merged_accounts: [
        {
          provider_id: 'openrouter',
          account_id: 'alice@example.com',
          destination_account_id: 'bob test@example.com',
          merged_at: '2026-10-20T10:00:00+00:00',
        },
      ],
    } as never);
    renderWithProviders(<MergedSection />);

    await userEvent.click(await screen.findByRole('button'));
    expect(mockNavigate).toHaveBeenCalledWith(
      '/provider/openrouter?account=bob%20test%40example.com',
    );
  });

  it('renders nothing when no account was merged', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      fleet: [],
      generated_at: '2026-10-20T10:00:00+00:00',
    } as never);
    const { container } = renderWithProviders(<MergedSection />);
    await waitFor(() => expect(api.fetchFleetUsage).toHaveBeenCalled());
    expect(container.querySelector('[aria-label="Merged accounts"]')).toBeNull();
  });
});
