import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { render } from '@testing-library/react';
import { useRangeParam } from './useRangeParam';

function Probe({ fallbackKey }: { fallbackKey?: string }) {
  const [value, setRange] = useRangeParam(fallbackKey);
  const location = useLocation();
  return (
    <div>
      <span data-testid="value">{JSON.stringify(value)}</span>
      <span data-testid="search">{location.search}</span>
      <button type="button" onClick={() => setRange({ days: 30 })}>
        rolling
      </button>
      <button type="button" onClick={() => setRange({ days: 7 })}>
        default
      </button>
      <button type="button" onClick={() => setRange({ since: '2026-09-01', until: '2026-09-30' })}>
        absolute
      </button>
    </div>
  );
}

function renderProbe(route: string, fallbackKey?: string) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Probe fallbackKey={fallbackKey} />
    </MemoryRouter>,
  );
}

describe('useRangeParam', () => {
  it('defaults to last 7 days when the param is absent', () => {
    renderProbe('/history');
    expect(screen.getByTestId('value')).toHaveTextContent('{"days":7}');
    expect(screen.getByTestId('search')).toHaveTextContent('');
  });

  it('parses a custom range from the URL', () => {
    renderProbe('/history?range=30d');
    expect(screen.getByTestId('value')).toHaveTextContent('{"days":30}');
  });

  it('parses an absolute range from the URL', () => {
    renderProbe('/history?range=2026-09-01_2026-09-30');
    expect(screen.getByTestId('value')).toHaveTextContent(
      '{"since":"2026-09-01","until":"2026-09-30"}',
    );
  });

  it('writes the range param, omitting it for the default', async () => {
    renderProbe('/history');
    await userEvent.click(screen.getByRole('button', { name: 'rolling' }));
    expect(screen.getByTestId('search')).toHaveTextContent('range=30d');
    await userEvent.click(screen.getByRole('button', { name: 'default' }));
    expect(screen.getByTestId('search')).toHaveTextContent('');
    await userEvent.click(screen.getByRole('button', { name: 'absolute' }));
    expect(screen.getByTestId('search')).toHaveTextContent('range=2026-09-01_2026-09-30');
  });

  it('falls back to a legacy param when range is absent and clears it on change', async () => {
    renderProbe('/provider/claude?period=2026-03', 'period');
    expect(screen.getByTestId('value')).toHaveTextContent(
      '{"since":"2026-03-01","until":"2026-03-31"}',
    );
    await userEvent.click(screen.getByRole('button', { name: 'rolling' }));
    expect(screen.getByTestId('search')).toHaveTextContent('range=30d');
    expect(screen.getByTestId('search')).not.toHaveTextContent('period=');
  });

  it('prefers range over the legacy param', () => {
    renderProbe('/provider/claude?range=14d&period=2026-03', 'period');
    expect(screen.getByTestId('value')).toHaveTextContent('{"days":14}');
  });
});
