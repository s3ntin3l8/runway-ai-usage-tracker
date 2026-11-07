import { screen } from '@testing-library/react';
import * as api from '@/api/endpoints';
import type { CollectorStatus, ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { ProviderDetailDialog } from './ProviderDetailDialog';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

const claude = {
  provider_id: 'anthropic',
  name: 'Anthropic',
  enabled: true,
  api_key_set: false,
  session_cookie_set: false,
  accounts: [],
  account_count: 0,
} as unknown as ProviderConfig;

const row = (over: object) => ({
  account_id: 'holder@example.com',
  last_outcome: 'ok',
  last_cycle_at: Date.now() / 1000 - 600,
  last_events: 3,
  consecutive_failures: 0,
  backlog: false,
  healthy: true,
  ...over,
});

function show(status: CollectorStatus) {
  vi.mocked(api.fetchStatus).mockResolvedValue(status);
  return renderWithProviders(<ProviderDetailDialog provider={claude} onClose={vi.fn()} />);
}

describe('Claude cloud-runner health lines', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows a plain line for a healthy account', async () => {
    show({ claude_cloud: { enabled: true, accounts: [row({})] } });
    const line = (await screen.findByText(/holder@example.com/)).closest('p')!;
    expect(line).toHaveTextContent('OK');
    expect(line).toHaveTextContent('10m ago');
    expect(line).toHaveAttribute('data-healthy', 'true');
    expect(line).not.toHaveClass('text-warning');
  });

  it('shows a warning line for an unhealthy account', async () => {
    show({
      claude_cloud: {
        enabled: true,
        accounts: [row({ last_outcome: 'internal_error', consecutive_failures: 3, healthy: false })],
      },
    });
    const line = (await screen.findByText(/holder@example.com/)).closest('p')!;
    expect(line).toHaveTextContent('internal error');
    expect(line).toHaveTextContent('needs attention');
    expect(line).toHaveClass('text-warning');
  });

  it('shows nothing when there are no accounts', async () => {
    show({ claude_cloud: { enabled: true, accounts: [] } });
    await vi.waitFor(() => expect(api.fetchStatus).toHaveBeenCalled());
    expect(screen.queryByTestId('claude-cloud-health')).toBeNull();
  });

  it('shows nothing for other providers', () => {
    vi.mocked(api.fetchStatus).mockResolvedValue({
      claude_cloud: { enabled: true, accounts: [row({})] },
    });
    renderWithProviders(
      <ProviderDetailDialog provider={{ ...claude, provider_id: 'openai' }} onClose={vi.fn()} />,
    );
    expect(screen.queryByTestId('claude-cloud-health')).toBeNull();
  });
});
