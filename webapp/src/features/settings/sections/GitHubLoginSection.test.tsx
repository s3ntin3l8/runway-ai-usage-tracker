import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { GitHubLoginSection } from './GitHubLoginSection';

vi.mock('@/api/endpoints');

const DEVICE = {
  device_code: 'dev',
  user_code: 'WXYZ-9876',
  verification_uri: 'https://github.com/login/device',
  expires_in: 900,
  interval: 0,
};

async function startFlow({ expectCode = true } = {}) {
  vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
  vi.mocked(api.initGitHubOAuth).mockResolvedValue(DEVICE);
  renderWithProviders(<GitHubLoginSection />);
  await userEvent.click(await screen.findByRole('button', { name: /connect via github/i }));
  if (expectCode) await screen.findByText(DEVICE.user_code);
}

describe('GitHubLoginSection', () => {
  afterEach(() => vi.clearAllMocks());

  it('offers sign-in when not authenticated and shows the device code', async () => {
    vi.mocked(api.pollGitHubOAuth).mockResolvedValue({ status: 'authorization_pending' });
    await startFlow();

    expect(screen.getByRole('link', { name: 'github.com/login/device' })).toHaveAttribute(
      'href',
      'https://github.com/login/device',
    );
  });

  it('shows the connected account and disconnects', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({
      authenticated: true,
      account: 's3ntin3l8',
      email: 'me@example.com',
    });
    vi.mocked(api.logoutGitHub).mockResolvedValue({});
    renderWithProviders(<GitHubLoginSection />);

    expect(await screen.findByText('@s3ntin3l8')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /disconnect/i }));

    await waitFor(() => expect(api.logoutGitHub).toHaveBeenCalled());
  });

  it('shows an error and lets the user retry when init fails', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockRejectedValue(new Error('GitHub unreachable'));
    renderWithProviders(<GitHubLoginSection />);

    await userEvent.click(await screen.findByRole('button', { name: /connect via github/i }));

    expect(await screen.findByText('GitHub unreachable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('returns to the connect state and refreshes status once authorised', async () => {
    const poll = vi.mocked(api.pollGitHubOAuth);
    poll.mockResolvedValueOnce({ status: 'authorization_pending' });
    poll.mockResolvedValueOnce({ status: 'success' });
    await startFlow();

    await waitFor(() => expect(poll).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText(DEVICE.user_code)).not.toBeInTheDocument());
    await waitFor(() =>
      expect(vi.mocked(api.getGitHubOAuthStatus).mock.calls.length).toBeGreaterThan(1),
    );
  });

  it('honours slow_down and keeps polling', async () => {
    const poll = vi.mocked(api.pollGitHubOAuth);
    poll.mockResolvedValueOnce({ status: 'slow_down', interval: 0 });
    poll.mockResolvedValueOnce({ status: 'success' });
    await startFlow();

    await waitFor(() => expect(poll).toHaveBeenCalledTimes(2));
  });

  it('shows an error when authorisation fails', async () => {
    vi.mocked(api.pollGitHubOAuth).mockRejectedValue(new Error('denied'));
    await startFlow({ expectCode: false });

    expect(await screen.findByText(/authorisation failed/i)).toBeInTheDocument();
  });

  it('cancels a pending flow and stops polling', async () => {
    const poll = vi.mocked(api.pollGitHubOAuth);
    poll.mockResolvedValue({ status: 'authorization_pending' });
    await startFlow();

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    expect(screen.queryByText(DEVICE.user_code)).not.toBeInTheDocument();
    const calls = poll.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(poll.mock.calls.length).toBe(calls);
  });

  it('does not start polling when the dialog closed while the code was requested', async () => {
    let resolveInit: (value: typeof DEVICE) => void = () => {};
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockReturnValue(
      new Promise((resolve) => {
        resolveInit = resolve;
      }),
    );
    const { unmount } = renderWithProviders(<GitHubLoginSection />);
    await userEvent.click(await screen.findByRole('button', { name: /connect via github/i }));

    unmount();
    resolveInit(DEVICE);
    await new Promise((resolve) => setTimeout(resolve, 30));

    expect(api.pollGitHubOAuth).not.toHaveBeenCalled();
  });

  it('copies the code to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    vi.mocked(api.pollGitHubOAuth).mockResolvedValue({ status: 'authorization_pending' });
    await startFlow();

    await userEvent.click(screen.getByTitle('Copy code'));

    expect(writeText).toHaveBeenCalledWith(DEVICE.user_code);
  });
});
