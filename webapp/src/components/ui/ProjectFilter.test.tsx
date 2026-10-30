import { screen } from '@testing-library/react';
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
    await userEvent.click(await screen.findByRole('option', { name: 'sanctuary' }));
    expect(screen.getByTestId('search')).toHaveTextContent('project=sanctuary');
    expect(screen.getByRole('option', { name: 'sanctuary' })).toHaveAttribute('aria-selected', 'true');
  });

  it('narrows the list as you type, without the all entry', async () => {
    renderFilter();
    await screen.findByRole('option', { name: 'runway' });
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search projects' }), 'SAN');
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['sanctuary', 'sandbox']);
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search projects' }), 'zzz');
    expect(screen.queryAllByRole('option')).toHaveLength(0);
    expect(screen.getByText(/no matching projects/i)).toBeInTheDocument();
  });

  it('clears with All projects and keeps a URL project the window no longer lists', async () => {
    renderFilter('/?project=gone');
    expect(await screen.findByRole('option', { name: 'gone' })).toHaveAttribute('aria-selected', 'true');
    await userEvent.click(screen.getByRole('option', { name: 'All projects' }));
    expect(screen.getByTestId('search')).not.toHaveTextContent('project');
  });
});
