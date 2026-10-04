import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { GitHubLoginSection } from './GitHubLoginSection';

vi.mock('@/api/endpoints');

describe('GitHubLoginSection', () => {
  afterEach(() => vi.clearAllMocks());

  it('offers sign-in when not authenticated and shows the device code', async () => {
    vi.mocked(api.getGitHubOAuthStatus).mockResolvedValue({ authenticated: false });
    vi.mocked(api.initGitHubOAuth).mockResolvedValue({
      device_code: 'dev',
      user_code: 'ABCD-1234',
      verification_uri: 'https://github.com/login/device',
      expires_in: 900,
      interval: 60,
    });
    renderWithProviders(<GitHubLoginSection />);

    await userEvent.click(await screen.findByRole('button', { name: /connect via github/i }));

    expect(await screen.findByText('ABCD-1234')).toBeInTheDocument();
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
});
