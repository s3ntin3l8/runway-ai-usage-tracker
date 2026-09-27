import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { DataHealthCheckReport, DataHealthReport } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { DataHealthSection } from './DataHealthSection';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const check = (o: Partial<DataHealthCheckReport> = {}): DataHealthCheckReport => ({
  check_id: 'config_default_keyed',
  severity: 'error',
  total_count: 0,
  fixable_count: 0,
  groups: [],
  blocked_by: [],
  blocked: false,
  ...o,
});

const report = (o: Partial<DataHealthReport> = {}): DataHealthReport => ({
  scanning: false,
  checks: [check()],
  ...o,
});

describe('DataHealthSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the all-clean empty state when every check has no findings', async () => {
    vi.mocked(api.fetchDataHealthReport).mockResolvedValue(report());
    renderWithProviders(<DataHealthSection />);
    expect(await screen.findByText(/all checks clean/i)).toBeInTheDocument();
  });

  it('renders one row per check when findings exist', async () => {
    vi.mocked(api.fetchDataHealthReport).mockResolvedValue(
      report({ checks: [check({ total_count: 3 }), check({ check_id: 'rollup_drift', severity: 'warn' })] }),
    );
    renderWithProviders(<DataHealthSection />);
    expect(await screen.findByText('config_default_keyed')).toBeInTheDocument();
    expect(screen.getByText('rollup_drift')).toBeInTheDocument();
  });

  it('sorts checks by severity, errors first', async () => {
    vi.mocked(api.fetchDataHealthReport).mockResolvedValue(
      report({
        checks: [
          check({ check_id: 'an_info_check', severity: 'info', total_count: 1 }),
          check({ check_id: 'a_error_check', severity: 'error', total_count: 1 }),
        ],
      }),
    );
    renderWithProviders(<DataHealthSection />);
    const rows = await screen.findAllByText(/_check$/);
    expect(rows[0]).toHaveTextContent('a_error_check');
    expect(rows[1]).toHaveTextContent('an_info_check');
  });

  it('shows a scanning indicator and disables re-scan while scanning', async () => {
    vi.mocked(api.fetchDataHealthReport).mockResolvedValue(report({ scanning: true }));
    renderWithProviders(<DataHealthSection />);
    expect(await screen.findByText(/scanning/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /re-scan/i })).toBeDisabled();
  });

  it('triggers a rescan when the button is clicked', async () => {
    vi.mocked(api.fetchDataHealthReport).mockResolvedValue(report());
    vi.mocked(api.rescanDataHealth).mockResolvedValue({ started: true });
    renderWithProviders(<DataHealthSection />);
    await screen.findByText(/all checks clean/i);
    await userEvent.click(screen.getByRole('button', { name: /re-scan/i }));
    expect(api.rescanDataHealth).toHaveBeenCalled();
  });

  it('shows an error state with a retry button on fetch failure', async () => {
    vi.mocked(api.fetchDataHealthReport).mockRejectedValue(new Error('boom'));
    renderWithProviders(<DataHealthSection />);
    expect(await screen.findByText(/could not load data health/i)).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
  });
});
