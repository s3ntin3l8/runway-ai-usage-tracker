import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { DataHealthCheckReport, DataHealthFindingGroup } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { CheckRow } from './CheckRow';

vi.mock('./FixDialog', () => ({
  FixDialog: ({ group }: { group: DataHealthFindingGroup }) => <div>Fixing {group.key}</div>,
}));

const group = (o: Partial<DataHealthFindingGroup> = {}): DataHealthFindingGroup => ({
  key: 'minimax',
  label: 'minimax: default → alice@example.com',
  count: 1,
  fixable: true,
  params: [],
  samples: [],
  detail: {},
  ...o,
});

const check = (o: Partial<DataHealthCheckReport> = {}): DataHealthCheckReport => ({
  check_id: 'config_default_keyed',
  title: 'Provider account uses a generic ID',
  description: 'A saved provider configuration uses a generic account ID.',
  impact: 'Usage can be split across identities.',
  recommended_action: 'Re-key the configuration.',
  severity: 'error',
  total_count: 1,
  fixable_count: 1,
  groups: [group()],
  blocked_by: [],
  blocked: false,
  ...o,
});

describe('CheckRow', () => {
  it('shows clean when there are no findings, collapsed by default', () => {
    renderWithProviders(<CheckRow check={check({ total_count: 0, groups: [] })} />);
    expect(screen.getByText('clean')).toBeInTheDocument();
    expect(screen.queryByText(/finding\(s\)/i)).not.toBeInTheDocument();
  });

  it('shows the finding count when there are findings', () => {
    renderWithProviders(<CheckRow check={check()} />);
    expect(screen.getByText('1 finding')).toBeInTheDocument();
  });

  it('expands to show groups on click', async () => {
    renderWithProviders(<CheckRow check={check()} />);
    expect(screen.queryByText(group().label)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /provider account uses a generic id/i }));
    expect(screen.getByText(group().label)).toBeInTheDocument();
  });

  it('shows a not-fixable reason instead of a Fix button when the group cannot be fixed', async () => {
    renderWithProviders(
      <CheckRow
        check={check({
          groups: [group({ fixable: false, not_fixable_reason: 'no configured account' })],
        })}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /provider account uses a generic id/i }));
    expect(screen.getByText('no configured account')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^fix$/i })).not.toBeInTheDocument();
  });

  it('opens the FixDialog when Fix is clicked', async () => {
    renderWithProviders(<CheckRow check={check()} />);
    await userEvent.click(screen.getByRole('button', { name: /provider account uses a generic id/i }));
    await userEvent.click(screen.getByRole('button', { name: /^fix$/i }));
    expect(screen.getByText('Fixing minimax')).toBeInTheDocument();
  });

  it('disables Fix when the check is blocked', async () => {
    renderWithProviders(
      <CheckRow
        check={check({ blocked: true, blocked_by: ['other_check'] })}
        checkNames={{ other_check: 'Prerequisite check' }}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /provider account uses a generic id/i }));
    expect(screen.getByRole('button', { name: /^fix$/i })).toBeDisabled();
    expect(screen.getByText('Fix first: Prerequisite check.')).toBeInTheDocument();
  });

  it('links non-fixable pending work to Fleet', async () => {
    renderWithProviders(
      <CheckRow
        check={check({
          check_id: 'pending_events',
          title: 'Events need account assignment',
          groups: [
            group({
              fixable: false,
              not_fixable_reason: 'Assign each event in Fleet',
              detail: { link: '/fleet#pending-events' },
            }),
          ],
        })}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /events need account assignment/i }));
    expect(screen.getByRole('link', { name: /open fleet/i })).toHaveAttribute(
      'href',
      '/fleet#pending-events',
    );
  });

  it('shows samples when Show samples is clicked', async () => {
    renderWithProviders(
      <CheckRow
        check={check({
          groups: [group({ samples: [{ label: 'minimax', detail: { account_id: 'default' } }] })],
        })}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /provider account uses a generic id/i }));
    await userEvent.click(screen.getByRole('button', { name: /show samples/i }));
    expect(screen.getByText('account_id:')).toBeInTheDocument();
    expect(screen.getByText('default')).toBeInTheDocument();
  });
});
