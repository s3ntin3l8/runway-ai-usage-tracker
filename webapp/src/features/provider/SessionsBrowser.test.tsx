import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { SessionsBrowser } from './SessionsBrowser';
import { currentPeriod, pastPeriod, sessionsPaginated } from './test-fixtures';
import { useLocation } from 'react-router';
import { useUsageSource } from '@/hooks/useUsageSource';
import { useModelFilter } from '@/hooks/useSessionFilters';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

function primeProjects(projects: string[] = ['runway', 'sanctuary']) {
  vi.mocked(api.fetchProjects).mockResolvedValue({ projects });
  vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
}

function SourceSwitch() {
  const [, setSidecar] = useUsageSource();
  const location = useLocation();
  return (
    <>
      <button type="button" onClick={() => setSidecar('other')}>
        switch source
      </button>
      <span data-testid="search">{location.search}</span>
    </>
  );
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
        scope={currentPeriod()}
        active={false}
      />,
    );
    expect(api.fetchSessionsPaginated).not.toHaveBeenCalled();
  });

  it('shows the empty state when no sessions this month', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(0, 0));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />,
    );
    expect(await screen.findByText(/no sessions in/i)).toBeInTheDocument();
  });

  it('renders a page of sessions with the range counter', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />,
    );
    expect(await screen.findByText(/^Sessions ·/)).toBeInTheDocument();
    expect(await screen.findByText(/showing 1–25 of 60/i)).toBeInTheDocument();
  });

  it('pages forward', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />,
    );
    const next = await screen.findByRole('button', { name: /next page/i });
    expect(screen.getByRole('button', { name: /previous page/i })).toBeDisabled();
    await userEvent.click(next);
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(expect.objectContaining({ page: 1 })),
    );
  });

  it('sends the URL project to the request and no longer renders its own select', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />,
      { route: '/provider/anthropic?tab=sessions&project=sanctuary' },
    );
    await screen.findByText(/^Sessions ·/);
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(
        expect.objectContaining({ project: 'sanctuary' }),
      ),
    );
  });

  it('sends no project when none is selected', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />,
    );
    await waitFor(() => expect(api.fetchSessionsPaginated).toHaveBeenCalled());
    expect(vi.mocked(api.fetchSessionsPaginated).mock.calls[0]?.[0]).not.toHaveProperty('project');
  });

  it('clears the project when the source changes, but not on mount', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <>
        <SourceSwitch />
        <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />
      </>,
      { route: '/provider/anthropic?sidecar=first&project=sanctuary' },
    );
    await screen.findByText(/^Sessions ·/);
    expect(screen.getByTestId('search')).toHaveTextContent('project=sanctuary');
    await userEvent.click(screen.getByRole('button', { name: 'switch source' }));
    await waitFor(() => expect(screen.getByTestId('search')).not.toHaveTextContent('project='));
    expect(screen.getByTestId('search')).toHaveTextContent('sidecar=other');
  });

  it('sends model, branch and subagents=exclude from the URL, and omits them otherwise', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />,
      { route: '/provider/anthropic?tab=sessions&model=opus&branch=main&subagents=exclude&project=__none__' },
    );
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(
        expect.objectContaining({ model: 'opus', branch: 'main', subagents: 'exclude', project: '__none__' }),
      ),
    );
  });

  it('sends none of the new filters when unset', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    renderWithProviders(
      <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />,
    );
    await waitFor(() => expect(api.fetchSessionsPaginated).toHaveBeenCalled());
    const params = vi.mocked(api.fetchSessionsPaginated).mock.calls[0]?.[0];
    expect(params).not.toHaveProperty('model');
    expect(params).not.toHaveProperty('branch');
    expect(params).not.toHaveProperty('subagents');
  });

  it('returns to page 0 when a filter changes', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(25, 60));
    function ModelSwitch() {
      const [, setModel] = useModelFilter();
      return <button type="button" onClick={() => setModel('opus')}>set model</button>;
    }
    renderWithProviders(
      <>
        <ModelSwitch />
        <SessionsBrowser providerId="anthropic" accountId="me@example.com" scope={currentPeriod()} active />
      </>,
    );
    await userEvent.click(await screen.findByRole('button', { name: /next page/i }));
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(expect.objectContaining({ page: 1 })),
    );
    await userEvent.click(screen.getByRole('button', { name: 'set model' }));
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenLastCalledWith(
        expect.objectContaining({ page: 0, model: 'opus' }),
      ),
    );
  });

  it('scopes the query to a past month with since and until', async () => {
    vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(sessionsPaginated(0, 0));
    renderWithProviders(
      <SessionsBrowser
        providerId="anthropic"
        accountId="me@example.com"
        scope={pastPeriod('2026-01')}
        active
      />,
    );
    const since = new Date(2026, 0, 1).toISOString();
    const until = new Date(2026, 1, 1).toISOString();
    await waitFor(() =>
      expect(api.fetchSessionsPaginated).toHaveBeenCalledWith(
        expect.objectContaining({ since, until }),
      ),
    );
  });
});
