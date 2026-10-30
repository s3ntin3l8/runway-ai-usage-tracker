import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { ProjectFilter } from './ProjectFilter';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

function Search() {
  return <span data-testid="search">{useLocation().search}</span>;
}

const renderFilter = (route = '/') =>
  renderWithProviders(
    <>
      <ProjectFilter providerId="anthropic" />
      <Search />
    </>,
    { route },
  );

describe('ProjectFilter', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchProjects).mockResolvedValue({ projects: ['runway', 'sanctuary', 'sandbox'] });
  });

  it('lists all projects and selects one into the URL', async () => {
    renderFilter();
    await userEvent.click(await screen.findByRole('button', { name: 'sanctuary' }));
    expect(screen.getByTestId('search')).toHaveTextContent('project=sanctuary');
    expect(screen.getByRole('button', { name: 'sanctuary' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('narrows the list as you type, without the all entry', async () => {
    renderFilter();
    await screen.findByRole('button', { name: 'runway' });
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search projects' }), 'SAN');
    const group = screen.getByRole('group', { name: 'Project filter' });
    const names = () => within(group).getAllByRole('button').map((b) => b.textContent);
    expect(names()).toEqual(['All projects', 'sanctuary', 'sandbox']);
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search projects' }), 'zzz');
    expect(names()).toEqual(['All projects']);
    expect(screen.getByText(/no matching projects/i)).toBeInTheDocument();
  });

  it('clears with All projects and keeps a URL project the window no longer lists', async () => {
    renderFilter('/?project=gone');
    expect(await screen.findByRole('button', { name: 'gone' })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search projects' }), 'run');
    await userEvent.click(screen.getByRole('button', { name: 'All projects' }));
    expect(screen.getByTestId('search')).not.toHaveTextContent('project');
  });

  it('renders nothing when the provider has no projects', async () => {
    vi.mocked(api.fetchProjects).mockResolvedValue({ projects: [] });
    renderFilter();
    await waitFor(() => expect(api.fetchProjects).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Project filter' })).not.toBeInTheDocument());
  });
});
