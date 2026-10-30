import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { SessionOptionFilter } from './SessionOptionFilter';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

function Search() {
  return <span data-testid="search">{useLocation().search}</span>;
}

const renderFilter = (kind: 'model' | 'branch', route = '/') =>
  renderWithProviders(
    <>
      <SessionOptionFilter kind={kind} providerId="anthropic" accountId="me" range={{ since: '2026-01-01T00:00:00Z', until: '2026-02-01T00:00:00Z' }} />
      <Search />
    </>,
    { route },
  );

describe('SessionOptionFilter', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchSessionModels).mockResolvedValue({ models: ['opus', 'sonnet'] });
    vi.mocked(api.fetchSessionBranches).mockResolvedValue({ branches: ['main', 'feat/x'] });
  });

  it('lists the models for the account and window and selects one into the URL', async () => {
    renderFilter('model');
    await userEvent.click(await screen.findByRole('button', { name: 'sonnet' }));
    expect(screen.getByTestId('search')).toHaveTextContent('model=sonnet');
    expect(screen.getByRole('button', { name: 'sonnet' })).toHaveAttribute('aria-pressed', 'true');
    expect(api.fetchSessionModels).toHaveBeenCalledWith(
      expect.objectContaining({ provider_id: 'anthropic', account_id: 'me', since: '2026-01-01T00:00:00Z' }),
    );
  });

  it('lists branches and clears with the pinned all entry', async () => {
    renderFilter('branch', '/?branch=feat/x');
    expect(await screen.findByRole('button', { name: 'feat/x' })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(screen.getByRole('button', { name: 'All branches' }));
    expect(screen.getByTestId('search')).not.toHaveTextContent('branch');
  });

  it('scopes the option list to the selected project', async () => {
    renderFilter('model', '/?project=__none__');
    await screen.findByRole('button', { name: 'opus' });
    expect(api.fetchSessionModels).toHaveBeenCalledWith(expect.objectContaining({ project: '__none__' }));
  });

  it('searches server-side after a debounce', async () => {
    renderFilter('branch');
    await screen.findByRole('button', { name: 'main' });
    vi.mocked(api.fetchSessionBranches).mockResolvedValue({ branches: ['feat/x'] });
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search branches' }), 'feat');
    await waitFor(() =>
      expect(api.fetchSessionBranches).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'feat' })),
    );
    const group = screen.getByRole('group', { name: 'Branch filter' });
    await waitFor(() =>
      expect(within(group).getAllByRole('button').map((b) => b.textContent)).toEqual(['All branches', 'feat/x']),
    );
  });

  it('keeps a selected value that is no longer listed visible while it matches the search', async () => {
    renderFilter('model', '/?model=gone');
    expect(await screen.findByRole('button', { name: 'gone' })).toHaveAttribute('aria-pressed', 'true');
    vi.mocked(api.fetchSessionModels).mockResolvedValue({ models: ['opus'] });
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search models' }), 'go');
    await waitFor(() => expect(api.fetchSessionModels).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'go' })));
    expect(screen.getByRole('button', { name: 'gone' })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search models' }), 'zz');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'gone' })).not.toBeInTheDocument());
  });

  it('shows an empty-search message', async () => {
    renderFilter('model');
    await screen.findByRole('button', { name: 'opus' });
    vi.mocked(api.fetchSessionModels).mockResolvedValue({ models: [] });
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search models' }), 'zzz');
    expect(await screen.findByText('No matching models')).toBeInTheDocument();
  });

  it('renders nothing when there are no values', async () => {
    vi.mocked(api.fetchSessionModels).mockResolvedValue({ models: [] });
    renderFilter('model');
    await waitFor(() => expect(api.fetchSessionModels).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Model filter' })).not.toBeInTheDocument());
  });

  it('reports a failed load', async () => {
    vi.mocked(api.fetchSessionModels).mockRejectedValue(new Error('boom'));
    renderFilter('model', '/');
    expect(await screen.findByText('Unavailable')).toBeInTheDocument();
  });
});
