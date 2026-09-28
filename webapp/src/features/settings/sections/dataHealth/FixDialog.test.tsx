import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { DataHealthFindingGroup } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { FixDialog } from './FixDialog';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

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

function renderDialog(g: DataHealthFindingGroup = group()) {
  return renderWithProviders(
    <FixDialog open onOpenChange={() => {}} checkId="config_default_keyed" group={g} />,
  );
}

describe('FixDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a param picker for an options-backed param', () => {
    renderDialog(
      group({
        params: [{ name: 'target', label: 'Target account', required: true, options: ['a', 'b'] }],
      }),
    );
    expect(screen.getByText('Target account')).toBeInTheDocument();
  });

  it('renders a text input for a param with no options', () => {
    renderDialog(
      group({ params: [{ name: 'new_account_id', label: 'New account id', required: false }] }),
    );
    expect(screen.getByLabelText(/new account id/i)).toBeInTheDocument();
  });

  it('runs a preview and shows the summary and counts', async () => {
    vi.mocked(api.previewDataHealthFix).mockResolvedValue({
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      summary: 'Rekey minimax/default onto alice@example.com',
      counts: { credential_tags: 2 },
      samples: [],
    });
    renderDialog();
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    expect(await screen.findByText('Rekey minimax/default onto alice@example.com')).toBeInTheDocument();
    expect(screen.getByText('credential_tags:')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('keeps Apply disabled until a preview has run and confirm is checked', async () => {
    vi.mocked(api.previewDataHealthFix).mockResolvedValue({
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      summary: 'Rekey it',
      counts: {},
      samples: [],
    });
    renderDialog();
    expect(screen.getByRole('button', { name: /apply fix/i })).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    await screen.findByText('Rekey it');
    expect(screen.getByRole('button', { name: /apply fix/i })).toBeDisabled(); // not confirmed yet

    await userEvent.click(screen.getByRole('switch'));
    expect(screen.getByRole('button', { name: /apply fix/i })).not.toBeDisabled();
  });

  it('requires same-account attestation after showing the collision preview', async () => {
    vi.mocked(api.previewDataHealthFix).mockResolvedValue({
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      summary: 'Archive minimax/default and keep alice@example.com',
      counts: { usage_events_retained_on_default: 3 },
      samples: [{ label: 'minimax/alice@example.com (target)', detail: { account_label: 'alice@example.com' } }],
      confirmation_text: 'I confirm minimax/default and minimax/alice@example.com are the same provider account.',
    });
    renderDialog();
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    expect(await screen.findByText(/same provider account/i)).toBeInTheDocument();
    expect(screen.getByText('minimax/alice@example.com (target)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('switch'));
    expect(screen.getByRole('button', { name: /apply fix/i })).toBeDisabled();
    await userEvent.click(screen.getByRole('checkbox'));
    expect(screen.getByRole('button', { name: /apply fix/i })).not.toBeDisabled();
  });

  it('re-locks Apply if a param changes after preview', async () => {
    vi.mocked(api.previewDataHealthFix).mockResolvedValue({
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      summary: 'Rekey it',
      counts: {},
      samples: [],
    });
    renderDialog(
      group({ params: [{ name: 'new_account_id', label: 'New account id', required: false }] }),
    );
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    await screen.findByText('Rekey it');
    await userEvent.click(screen.getByRole('switch'));
    expect(screen.getByRole('button', { name: /apply fix/i })).not.toBeDisabled();

    await userEvent.type(screen.getByLabelText(/new account id/i), 'x');
    expect(screen.getByRole('button', { name: /apply fix/i })).toBeDisabled();
  });

  it('shows a preview error', async () => {
    vi.mocked(api.previewDataHealthFix).mockRejectedValue(new Error('no such group'));
    renderDialog();
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    expect(await screen.findByText('no such group')).toBeInTheDocument();
  });

  it('applies the fix and shows job progress through to success', async () => {
    vi.mocked(api.previewDataHealthFix).mockResolvedValue({
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      summary: 'Rekey it',
      counts: {},
      samples: [],
    });
    vi.mocked(api.applyDataHealthFix).mockResolvedValue({ job_id: 'job-1' });
    vi.mocked(api.fetchDataHealthJob).mockResolvedValue({
      id: 'job-1',
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      status: 'succeeded',
      result: {
        check_id: 'config_default_keyed',
        group_key: 'minimax',
        summary: 'Rekeyed minimax/default',
        counts: { credential_tags_moved: 1 },
      },
      error: null,
      started_at: '2026-09-28T00:00:00Z',
      finished_at: '2026-09-28T00:00:01Z',
    });

    renderDialog();
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    await screen.findByText('Rekey it');
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByRole('button', { name: /apply fix/i }));

    expect(await screen.findByText('Rekeyed minimax/default')).toBeInTheDocument();
    expect(screen.getByText('credential_tags_moved:')).toBeInTheDocument();
  });

  it('shows a job failure', async () => {
    vi.mocked(api.previewDataHealthFix).mockResolvedValue({
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      summary: 'Rekey it',
      counts: {},
      samples: [],
    });
    vi.mocked(api.applyDataHealthFix).mockResolvedValue({ job_id: 'job-1' });
    vi.mocked(api.fetchDataHealthJob).mockResolvedValue({
      id: 'job-1',
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      status: 'failed',
      result: null,
      error: 'target is not a configured account',
      started_at: '2026-09-28T00:00:00Z',
      finished_at: '2026-09-28T00:00:01Z',
    });

    renderDialog();
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    await screen.findByText('Rekey it');
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByRole('button', { name: /apply fix/i }));

    expect(await screen.findByText('target is not a configured account')).toBeInTheDocument();
  });

  it('keeps a job open with unknown status and offers a retry if polling fails', async () => {
    vi.mocked(api.previewDataHealthFix).mockResolvedValue({
      check_id: 'config_default_keyed',
      group_key: 'minimax',
      summary: 'Rekey it',
      counts: {},
      samples: [],
    });
    vi.mocked(api.applyDataHealthFix).mockResolvedValue({ job_id: 'job-1' });
    vi.mocked(api.fetchDataHealthJob).mockRejectedValue(new Error('network error'));

    renderDialog();
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    await screen.findByText('Rekey it');
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByRole('button', { name: /apply fix/i }));

    expect(await screen.findByText(/network error/)).toBeInTheDocument();
    expect(screen.getByText(/status is unknown/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry status/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /close/i })).not.toBeInTheDocument();
  });
});
