import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { Banners } from '@/features/home/Banners';
import { CredentialsSection } from '@/features/settings/sections/credentials/CredentialsSection';
import { useCredentialInventory } from '@/features/settings/sections/credentials/queries';
import { inventory } from '@/features/settings/sections/credentials/testData';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const notice = {
  providers: [
    { provider_id: 'anthropic', name: 'Claude Pro' },
    { provider_id: 'chatgpt', name: 'ChatGPT' },
  ],
  removed_count: 2,
  recorded_at: '2026-10-11T12:00:00Z',
};
function HomeNotices() {
  const credentials = useCredentialInventory();
  return <Banners credentials={credentials.data} anomalies={[]} />;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(api.fetchCredentialInventory).mockResolvedValue(
    inventory({ credential_file_migration_notice: notice }),
  );
});

it.each(['Home', 'Credentials'])(
  'shows the same migration guidance on %s and dismisses after server acknowledgement',
  async (surface) => {
    vi.mocked(api.dismissCredentialFileMigrationNotice).mockImplementation(async () => {
      vi.mocked(api.fetchCredentialInventory).mockResolvedValue(inventory());
      return { status: 'ok' };
    });
    renderWithProviders(surface === 'Home' ? <HomeNotices /> : <CredentialsSection />);
    expect(
      await screen.findByText(/legacy server login-file sources for Claude Pro, ChatGPT/),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Set up a sidecar' })).toHaveAttribute(
      'href',
      '/fleet',
    );
    expect(screen.getByRole('link', { name: 'add a key in Providers' })).toHaveAttribute(
      'href',
      '/settings/providers',
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Dismiss login-file migration notice' }),
    );
    await waitFor(() =>
      expect(screen.queryByText(/legacy server login-file sources/)).not.toBeInTheDocument(),
    );
    expect(api.dismissCredentialFileMigrationNotice).toHaveBeenCalledTimes(1);
    expect(localStorage.length).toBe(0);
  },
);

it('keeps the notice visible when persistence fails', async () => {
  vi.mocked(api.dismissCredentialFileMigrationNotice).mockRejectedValue(new Error('offline'));
  renderWithProviders(<HomeNotices />);
  await userEvent.click(
    await screen.findByRole('button', { name: 'Dismiss login-file migration notice' }),
  );
  await waitFor(() =>
    expect(toast.error).toHaveBeenCalledWith('Could not dismiss notice: offline'),
  );
  expect(screen.getByText(/legacy server login-file sources/)).toBeInTheDocument();
});

it('waits for the server and disables duplicate dismissals', async () => {
  let resolve!: (value: { status: string }) => void;
  vi.mocked(api.dismissCredentialFileMigrationNotice).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  renderWithProviders(<HomeNotices />);
  const button = await screen.findByRole('button', { name: 'Dismiss login-file migration notice' });
  await userEvent.click(button);
  expect(button).toBeDisabled();
  expect(screen.getByText(/legacy server login-file sources/)).toBeInTheDocument();
  vi.mocked(api.fetchCredentialInventory).mockResolvedValue(inventory());
  resolve({ status: 'ok' });
  await waitFor(() =>
    expect(screen.queryByText(/legacy server login-file sources/)).not.toBeInTheDocument(),
  );
});

it('shows no notice on fresh or previously migrated installations', async () => {
  vi.mocked(api.fetchCredentialInventory).mockResolvedValue(inventory());
  renderWithProviders(<CredentialsSection />);
  await screen.findByText('No credentials found yet');
  expect(screen.queryByText(/legacy server login-file sources/)).not.toBeInTheDocument();
});
