import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { ArchivedSection } from './ArchivedSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

const mockArchived = [
  {
    provider_id: 'anthropic',
    account_id: 'u@example.com',
    lifetime: {
      tokens_input: 1000,
      tokens_output: 2000,
      tokens_cache_read: 500,
      tokens_cache_create: 200,
      tokens_reasoning: 100,
      msgs: 42,
      cost_usd: 5.67,
      by_model: {},
    },
    last_activity_ts: '2026-03-15T10:00:00Z',
  },
  {
    provider_id: 'gemini',
    account_id: 'default',
    lifetime: {
      tokens_input: 500,
      tokens_output: 300,
      tokens_cache_read: 0,
      tokens_cache_create: 0,
      tokens_reasoning: 0,
      msgs: 10,
      cost_usd: 1.23,
      by_model: {},
    },
    last_activity_ts: '2026-02-10T08:00:00Z',
  },
];

describe('ArchivedSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchArchivedProviders).mockResolvedValue({ archived: mockArchived } as never);
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        { provider_id: 'anthropic', name: 'Claude' } as never,
        { provider_id: 'gemini', name: 'Gemini' } as never,
      ],
    });
  });

  it('renders the Archived heading and provider names', async () => {
    renderWithProviders(<ArchivedSection />);
    expect(await screen.findByText('Archived')).toBeInTheDocument();
    expect(screen.getByText('Claude')).toBeInTheDocument();
    expect(screen.getByText('Gemini')).toBeInTheDocument();
  });

  it('renders lifetime stats for each card', async () => {
    renderWithProviders(<ArchivedSection />);
    await screen.findByText('Archived');
    expect(screen.getAllByText('Tokens').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Msgs').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Cost').length).toBeGreaterThanOrEqual(2);
  });

  it('uses the same responsive grid as the provider grid', async () => {
    const { container } = renderWithProviders(<ArchivedSection />);
    await screen.findByText('Archived');
    const grid = container.querySelector('[aria-label="Archived providers"] > div');
    expect(grid?.className).toContain('sm:grid-cols-2');
    expect(grid?.className).toContain('xl:grid-cols-3');
    expect(grid?.className).toContain('2xl:grid-cols-4');
  });
});
