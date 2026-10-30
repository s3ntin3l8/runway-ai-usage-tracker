import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { useBranchFilter, useHideSubagents, useModelFilter } from './useSessionFilters';

function Probe() {
  const [model, setModel] = useModelFilter();
  const [branch, setBranch] = useBranchFilter();
  const [hide, setHide] = useHideSubagents();
  const location = useLocation();
  return (
    <div>
      <span data-testid="value">{`${model ?? '-'}|${branch ?? '-'}|${hide}`}</span>
      <span data-testid="search">{location.search}</span>
      <button type="button" onClick={() => setModel('opus')}>model</button>
      <button type="button" onClick={() => setBranch('main')}>branch</button>
      <button type="button" onClick={() => setHide(true)}>hide</button>
      <button type="button" onClick={() => { setModel(undefined); setBranch(undefined); setHide(false); }}>clear</button>
    </div>
  );
}

const renderProbe = (route: string) =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <Probe />
    </MemoryRouter>,
  );

describe('useSessionFilters', () => {
  it('reads the filters from the URL and ignores empty or unknown values', () => {
    renderProbe('/p?model=opus&branch=&subagents=include');
    expect(screen.getByTestId('value')).toHaveTextContent('opus|-|false');
  });

  it('reads subagents=exclude', () => {
    renderProbe('/p?subagents=exclude');
    expect(screen.getByTestId('value')).toHaveTextContent('-|-|true');
  });

  it('sets and clears the params while keeping the others', async () => {
    renderProbe('/p?tab=sessions&sidecar=s1');
    await userEvent.click(screen.getByRole('button', { name: 'model' }));
    await userEvent.click(screen.getByRole('button', { name: 'branch' }));
    await userEvent.click(screen.getByRole('button', { name: 'hide' }));
    expect(screen.getByTestId('search')).toHaveTextContent(
      'tab=sessions&sidecar=s1&model=opus&branch=main&subagents=exclude',
    );
    await userEvent.click(screen.getByRole('button', { name: 'clear' }));
    expect(screen.getByTestId('search')).toHaveTextContent('?tab=sessions&sidecar=s1');
  });
});
