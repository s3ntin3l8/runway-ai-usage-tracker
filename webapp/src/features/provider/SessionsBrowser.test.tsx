import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { SessionsBrowser } from './SessionsBrowser';
import { currentPeriod, pastPeriod, sessionsPaginated } from './test-fixtures';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

function primeProjects(projects: string[] = ['runway', 'sanctuary']) {
  vi.mocked(api.fetchProjects).mockResolvedValue({ projects });
  vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
}

describe('SessionsBrowser', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    primeProjects();
  });

  it('does not fetch while inactive', () => {
    renderWithProviders(
      <SessionsBrowser
        providerId="anthropic"
        accountId="me@example.com"
        period={currentPeriod()}
        active={false}
      />,
    );
    expect(api.fetchSessionsPaginated).not.toHaveBeenCalled();
  });

  it('shows the empty state when no sessions this month', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(0, 0));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    expect(await screen.findByText(/no sessions in/i)).toBeInTheDocument();
  });

  it('renders a page of sessions with the range counter', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    expect(await screen.findByText(/^Sessions ·/)).toBeInTheDocument();
    expect(await screen.findByText(/showing 1–25 of 60/i)).toBeInTheDocument();
  });

  it('pages forward', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    const next = await screen.findByRole('button', { name: /next page/i });
    expect(screen.getByRole('button', { name: /previous page/i })).toBeDisabled();
    await userEvent.click(next);
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(expect.objectContaining({ page: 1 })),
    );
  });

  it('filters by project', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    await screen.findByText(/^Sessions ·/);
    // The filter renders once the projects query resolves.
    await userEvent.click(await screen.findByRole('combobox'));
    await userEvent.click(await screen.findByRole('option', { name: 'sanctuary' }));
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(
        expect.objectContaining({ project: 'sanctuary' }),
      ),
    );
  });

  it('scopes the query to a past month with since and until', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(0, 0));
    renderWithProviders(
      <SessionsBrowser
        providerId="anthropic"
        accountId="me@example.com"
        period={pastPeriod('2026-01')}
        active
      />,
    );
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(
        expect.objectContaining({ since: expect.stringContaining('2026-01'), until: expect.any(String) }),
      ),
    );
  });
});
