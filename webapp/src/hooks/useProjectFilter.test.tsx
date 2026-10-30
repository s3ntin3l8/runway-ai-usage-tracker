import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { useProjectFilter } from './useProjectFilter';

function Probe() {
  const [project, setProject] = useProjectFilter();
  const location = useLocation();
  return (
    <div>
      <span data-testid="value">{project ?? 'none'}</span>
      <span data-testid="search">{location.search}</span>
      <button type="button" onClick={() => setProject('alpha')}>
        set
      </button>
      <button type="button" onClick={() => setProject(undefined)}>
        clear
      </button>
    </div>
  );
}

const renderProbe = (route: string) =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <Probe />
    </MemoryRouter>,
  );

describe('useProjectFilter', () => {
  it('is undefined when the param is absent or empty', () => {
    renderProbe('/provider/x?project=');
    expect(screen.getByTestId('value')).toHaveTextContent('none');
  });

  it('reads the project from the URL', () => {
    renderProbe('/provider/x?project=beta');
    expect(screen.getByTestId('value')).toHaveTextContent('beta');
  });

  it('sets and clears the param while keeping the other params', async () => {
    renderProbe('/provider/x?tab=sessions&sidecar=s1');
    await userEvent.click(screen.getByRole('button', { name: 'set' }));
    expect(screen.getByTestId('search')).toHaveTextContent('tab=sessions&sidecar=s1&project=alpha');
    await userEvent.click(screen.getByRole('button', { name: 'clear' }));
    expect(screen.getByTestId('search')).toHaveTextContent('?tab=sessions&sidecar=s1');
    expect(screen.getByTestId('search')).not.toHaveTextContent('project');
  });
});
