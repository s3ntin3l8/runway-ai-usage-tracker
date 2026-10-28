import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import type { Sidecar, SidecarAdoptSummary, SidecarDescriptor } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { MergeSidecarNotice } from './MergeSidecarNotice';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const DEVICE = `device-${'a'.repeat(32)}`;
const legacyEntry: SidecarDescriptor = {
  sidecar_id: 'macbook',
  name: 'macbook',
  os_platform: 'Darwin/25.6.0',
  first_seen: '2026-06-14T18:52:19Z',
  last_seen: '2026-10-28T00:29:00Z',
  ingest_count: 54985,
};
const deviceEntry: SidecarDescriptor = {
  sidecar_id: DEVICE,
  name: 'macbook',
  os_platform: 'Darwin/25.6.0',
  first_seen: '2026-10-28T00:30:00Z',
  last_seen: '2026-10-28T08:00:00Z',
  ingest_count: 10,
};

// The paired device's card knows the old entry; the old entry's card knows the device.
const onDevice: Sidecar = { sidecar_id: DEVICE, adoptable_legacy: [legacyEntry] };
const onLegacy: Sidecar = {
  sidecar_id: 'macbook',
  hostname: 'macbook',
  custom_name: 'Office Mac',
  replaced_by: [deviceEntry],
};

const summary = (dry_run: boolean): SidecarAdoptSummary => ({
  dry_run,
  events: 44741,
  pending_events: 0,
  credential_tags: 19,
  settings: [],
  from: legacyEntry,
  into: deviceEntry,
  pushes_after: 54995,
});

describe('MergeSidecarNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(api.adoptLegacySidecar).mockImplementation(async (_id, _legacy, dryRun) => summary(dryRun));
  });

  it.each([
    ['the paired device card', onDevice, /Same machine as the old entry “macbook”\?/],
    ['the old entry card', onLegacy, /This machine was re-paired as “macbook”\./],
  ])('is offered on %s, worded for that side', (_label, sidecar, wording) => {
    renderWithProviders(<MergeSidecarNotice sidecar={sidecar} />);

    expect(screen.getByText(wording)).toBeInTheDocument();
    expect(api.adoptLegacySidecar).not.toHaveBeenCalled();
  });

  it.each([
    ['the paired device card', onDevice],
    ['the old entry card', onLegacy],
  ])('always merges the old entry into the paired device, from %s', async (_label, sidecar) => {
    const user = userEvent.setup();
    renderWithProviders(<MergeSidecarNotice sidecar={sidecar} />);

    await user.click(screen.getByRole('button', { name: /review merge/i }));

    // Same call either way: (device id, old entry id), never the other way round.
    await waitFor(() => expect(api.adoptLegacySidecar).toHaveBeenCalledWith(DEVICE, 'macbook', true));
    expect(await screen.findByText('Old entry · shared key')).toBeInTheDocument();
    expect(screen.getByText('Paired device')).toBeInTheDocument();
    expect(screen.getByText(/44,741 usage events move to the paired device/)).toBeInTheDocument();
    expect(screen.getByText(/19 credential tags/)).toBeInTheDocument();
    // Both entries are named "macbook"; the pushes line says exactly what the card becomes.
    expect(screen.getByText(/Pushes: 10 \+ 54,985 =/)).toBeInTheDocument();
    expect(screen.getByText('54,995')).toBeInTheDocument();
    expect(api.adoptLegacySidecar).not.toHaveBeenCalledWith(DEVICE, 'macbook', false);

    await user.click(screen.getByRole('button', { name: 'Merge into paired device' }));
    await waitFor(() => expect(api.adoptLegacySidecar).toHaveBeenCalledWith(DEVICE, 'macbook', false));
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
  });

  it('stays hidden once dismissed, from either card', async () => {
    const user = userEvent.setup();
    const { unmount } = renderWithProviders(<MergeSidecarNotice sidecar={onDevice} />);

    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(/Same machine as the old entry/)).not.toBeInTheDocument();
    unmount();

    // The dismissal is per pair, so the old entry's card honours it too.
    renderWithProviders(<MergeSidecarNotice sidecar={onLegacy} />);
    expect(screen.queryByText(/This machine was re-paired as/)).not.toBeInTheDocument();
  });

  it('renders nothing when there is no counterpart', () => {
    renderWithProviders(<MergeSidecarNotice sidecar={{ sidecar_id: 'dev-01' }} />);

    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  });

  it('surfaces a refusal from the server in the preview and cannot merge', async () => {
    const user = userEvent.setup();
    vi.mocked(api.adoptLegacySidecar).mockRejectedValue(new Error('not a legacy sidecar'));
    renderWithProviders(<MergeSidecarNotice sidecar={onDevice} />);

    await user.click(screen.getByRole('button', { name: /review merge/i }));

    expect(await screen.findByText('not a legacy sidecar')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Merge into paired device' })).toBeDisabled();
  });

  it('names the entry that goes away in the confirmation, from either card', async () => {
    const user = userEvent.setup();
    // On the old entry's card the entry that moves is this card ("Office Mac"), not the device.
    const { unmount } = renderWithProviders(<MergeSidecarNotice sidecar={onLegacy} />);
    await user.click(screen.getByRole('button', { name: /review merge/i }));
    await user.click(await screen.findByRole('button', { name: 'Merge into paired device' }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Merged Office Mac into the paired device'),
    );
    unmount();
    vi.mocked(toast.success).mockClear();

    // On the paired device's card it is the old entry the card points at.
    renderWithProviders(<MergeSidecarNotice sidecar={onDevice} />);
    await user.click(screen.getByRole('button', { name: /review merge/i }));
    await user.click(await screen.findByRole('button', { name: 'Merge into paired device' }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Merged macbook into the paired device'));
  });

  it('keeps a pair dismissed under the earlier "adopt" key dismissed', () => {
    window.localStorage.setItem(`runway.fleet.adopt-dismissed.${DEVICE}.macbook`, '1');

    renderWithProviders(<MergeSidecarNotice sidecar={onDevice} />);

    expect(screen.queryByText(/Same machine as the old entry/)).not.toBeInTheDocument();
  });
});
