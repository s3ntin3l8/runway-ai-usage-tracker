import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { AdoptLegacyNotice } from './AdoptLegacyNotice';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const DEVICE = `device-${'a'.repeat(32)}`;
const candidates = [{ sidecar_id: 'desktop', last_seen: '2026-10-27T21:01:00Z', ingest_count: 18689 }];
const summary = (dry_run: boolean) => ({
  dry_run,
  events: 25247,
  pending_events: 0,
  credential_tags: 2,
  settings: ['custom_name'],
});

describe('AdoptLegacyNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(api.adoptLegacySidecar).mockImplementation(async (_id, _legacy, dryRun) => summary(dryRun));
  });

  it('previews what would move before anything is written, then adopts on confirm', async () => {
    const user = userEvent.setup();
    renderWithProviders(<AdoptLegacyNotice sidecarId={DEVICE} candidates={candidates} />);

    expect(screen.getByText(/re-paired desktop/)).toBeInTheDocument();
    expect(api.adoptLegacySidecar).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: /review & adopt/i }));
    expect(await screen.findByText(/25.2K usage events move/)).toBeInTheDocument();
    expect(screen.getByText(/2 credential tags/)).toBeInTheDocument();
    expect(api.adoptLegacySidecar).toHaveBeenCalledWith(DEVICE, 'desktop', true);
    expect(api.adoptLegacySidecar).not.toHaveBeenCalledWith(DEVICE, 'desktop', false);

    await user.click(screen.getByRole('button', { name: 'Adopt' }));
    await waitFor(() => expect(api.adoptLegacySidecar).toHaveBeenCalledWith(DEVICE, 'desktop', false));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Adopted desktop'));
  });

  it('stays hidden once dismissed', async () => {
    const user = userEvent.setup();
    renderWithProviders(<AdoptLegacyNotice sidecarId={DEVICE} candidates={candidates} />);

    await user.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(screen.queryByText(/re-paired desktop/)).not.toBeInTheDocument();
    expect(api.adoptLegacySidecar).not.toHaveBeenCalled();
  });

  it('surfaces a refusal from the server in the preview', async () => {
    const user = userEvent.setup();
    vi.mocked(api.adoptLegacySidecar).mockRejectedValue(new Error('not a legacy sidecar'));
    renderWithProviders(<AdoptLegacyNotice sidecarId={DEVICE} candidates={candidates} />);

    await user.click(screen.getByRole('button', { name: /review & adopt/i }));

    expect(await screen.findByText('not a legacy sidecar')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Adopt' })).toBeDisabled();
  });
});
