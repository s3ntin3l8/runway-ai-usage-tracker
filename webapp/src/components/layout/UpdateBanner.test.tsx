import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { UpdateBanner } from './UpdateBanner';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

const base = {
  project_name: 'Runway',
  version: '2.1.0',
};

describe('UpdateBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('renders nothing when up to date', async () => {
    vi.mocked(api.fetchSettings).mockResolvedValue({
      ...base,
      latest_version: '2.1.0',
      update_available: false,
    });
    const { container } = renderWithProviders(<UpdateBanner />);
    // Give the query a tick to resolve; banner must stay absent.
    await waitFor(() => expect(api.fetchSettings).toHaveBeenCalled());
    expect(container.textContent).toBe('');
  });

  it('shows the available version when an update exists', async () => {
    vi.mocked(api.fetchSettings).mockResolvedValue({
      ...base,
      latest_version: '2.2.0',
      update_available: true,
    });
    renderWithProviders(<UpdateBanner />);
    expect(await screen.findByText('v2.2.0')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /release notes/i })).toBeInTheDocument();
  });

  it('stays hidden for a version already dismissed', async () => {
    localStorage.setItem('runway:update-dismissed:2.2.0', '1');
    vi.mocked(api.fetchSettings).mockResolvedValue({
      ...base,
      latest_version: '2.2.0',
      update_available: true,
    });
    const { container } = renderWithProviders(<UpdateBanner />);
    await waitFor(() => expect(api.fetchSettings).toHaveBeenCalled());
    expect(container.textContent).toBe('');
  });

  it('dismiss hides the banner and persists per-version', async () => {
    vi.mocked(api.fetchSettings).mockResolvedValue({
      ...base,
      latest_version: '2.2.0',
      update_available: true,
    });
    renderWithProviders(<UpdateBanner />);
    await screen.findByText('v2.2.0');

    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }));

    expect(screen.queryByText('v2.2.0')).not.toBeInTheDocument();
    expect(localStorage.getItem('runway:update-dismissed:2.2.0')).toBe('1');
  });
});
