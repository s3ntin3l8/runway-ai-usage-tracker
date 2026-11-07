import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import type { ProviderConfig } from '@/api/types';
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

function show(effective: boolean) {
  vi.mocked(api.fetchStatus).mockResolvedValue({ claude_cloud: { enabled: effective, accounts: [] } });
  vi.mocked(api.fetchAppConfig).mockResolvedValue({
    claude_cloud_collector_enabled: null,
    claude_cloud_collector_effective: effective,
  });
  return renderWithProviders(<ProviderDetailDialog provider={claude} onClose={vi.fn()} />);
}

describe('Claude cloud-runner collector switch', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders the current state with its description', async () => {
    show(true);
    const sw = await screen.findByRole('switch', { name: /cloud-runner collector/i });
    expect(sw).toBeChecked();
    expect(screen.getByTestId('claude-cloud-state')).toHaveTextContent('On');
    expect(
      screen.getByText("Collect usage for Claude Code sessions that run on Anthropic's cloud runner."),
    ).toBeInTheDocument();
  });

  it('reflects an off state', async () => {
    show(false);
    const sw = await screen.findByRole('switch', { name: /cloud-runner collector/i });
    await waitFor(() => expect(sw).not.toBeChecked());
    expect(screen.getByTestId('claude-cloud-state')).toHaveTextContent('Off');
  });

  it('sends the change when toggled', async () => {
    vi.mocked(api.putAppConfig).mockResolvedValue({ status: 'saved' });
    show(true);
    const sw = await screen.findByRole('switch', { name: /cloud-runner collector/i });
    await userEvent.click(sw);
    await waitFor(() =>
      expect(api.putAppConfig).toHaveBeenCalledWith({ claude_cloud_collector_enabled: false }),
    );
  });

  it('rolls back and shows an error when the save fails', async () => {
    vi.mocked(api.putAppConfig).mockRejectedValue(new Error('boom'));
    show(true);
    const sw = await screen.findByRole('switch', { name: /cloud-runner collector/i });
    await userEvent.click(sw);
    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
    expect(toast.error).toHaveBeenCalledWith('boom');
    await waitFor(() => expect(sw).toBeChecked());
  });

  it('is not shown for other providers', () => {
    vi.mocked(api.fetchStatus).mockResolvedValue({});
    vi.mocked(api.fetchAppConfig).mockResolvedValue({});
    renderWithProviders(
      <ProviderDetailDialog provider={{ ...claude, provider_id: 'openai' }} onClose={vi.fn()} />,
    );
    expect(screen.queryByTestId('claude-cloud-collector')).toBeNull();
  });
});
