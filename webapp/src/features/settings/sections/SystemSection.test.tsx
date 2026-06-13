import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AppConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { SystemSection } from './SystemSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const appConfig = (o: Partial<AppConfig> = {}): AppConfig => ({
  browser_preference: 'firefox',
  default_poll_interval_seconds: 120,
  user_timezone: 'Europe/Berlin',
  env_timezone: 'UTC',
  sidecar_update_channel: 'stable',
  ...o,
});

describe('SystemSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows a skeleton while app config loads', () => {
    vi.mocked(api.fetchAppConfig).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<SystemSection />);
    expect(container.querySelector('[class*="animate-pulse"]')).toBeTruthy();
  });

  it('populates the form fields from the loaded config', async () => {
    vi.mocked(api.fetchAppConfig).mockResolvedValue(appConfig());
    renderWithProviders(<SystemSection />);

    expect(await screen.findByLabelText(/timezone/i)).toHaveValue('Europe/Berlin');
    expect(screen.getByLabelText(/default poll interval/i)).toHaveValue(120);
    expect(screen.getByLabelText(/browser preference/i)).toHaveValue('firefox');
  });

  it('saves edited config via putAppConfig', async () => {
    vi.mocked(api.fetchAppConfig).mockResolvedValue(appConfig());
    vi.mocked(api.putAppConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<SystemSection />);

    const tz = await screen.findByLabelText(/timezone/i);
    await userEvent.clear(tz);
    await userEvent.type(tz, 'America/New_York');

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(api.putAppConfig).toHaveBeenCalledWith({
      user_timezone: 'America/New_York',
      default_poll_interval_seconds: 120,
      browser_preference: 'firefox',
      sidecar_update_channel: 'stable',
      sidecar_auto_update: false,
    });
  });

  it('changes the sidecar update channel and includes it in the save', async () => {
    vi.mocked(api.fetchAppConfig).mockResolvedValue(appConfig());
    vi.mocked(api.putAppConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<SystemSection />);

    await screen.findByLabelText(/timezone/i);
    await userEvent.click(screen.getByRole('combobox'));
    await userEvent.click(await screen.findByRole('option', { name: /edge/i }));

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(vi.mocked(api.putAppConfig).mock.calls[0][0]).toMatchObject({
      sidecar_update_channel: 'edge',
    });
  });

  it('reflects a loaded edge channel and keeps it on save (read-back regression)', async () => {
    // Guards the "channel resets on reload" bug: a config loaded as edge must
    // render as edge and survive a save unchanged.
    vi.mocked(api.fetchAppConfig).mockResolvedValue(appConfig({ sidecar_update_channel: 'edge' }));
    vi.mocked(api.putAppConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<SystemSection />);

    // Save without touching the selector: if the loaded 'edge' weren't read
    // into state, this would send the 'stable' useState default.
    await userEvent.click(await screen.findByRole('button', { name: /^save$/i }));
    expect(vi.mocked(api.putAppConfig).mock.calls[0][0]).toMatchObject({
      sidecar_update_channel: 'edge',
    });
  });

  it('triggers a force collect', async () => {
    vi.mocked(api.fetchAppConfig).mockResolvedValue(appConfig());
    vi.mocked(api.forceCollect).mockResolvedValue({
      ok: true,
      cards: 3,
      sidecars_triggered: 1,
    });
    renderWithProviders(<SystemSection />);

    await userEvent.click(await screen.findByRole('button', { name: /force collect/i }));
    expect(api.forceCollect).toHaveBeenCalled();
  });

  it('wakes the poller', async () => {
    vi.mocked(api.fetchAppConfig).mockResolvedValue(appConfig());
    vi.mocked(api.postWake).mockResolvedValue(undefined);
    renderWithProviders(<SystemSection />);

    await userEvent.click(await screen.findByRole('button', { name: /wake poller/i }));
    expect(api.postWake).toHaveBeenCalled();
  });

  it('runs database cleanup through the dialog', async () => {
    vi.mocked(api.fetchAppConfig).mockResolvedValue(appConfig());
    vi.mocked(api.postCleanup).mockResolvedValue({ ok: true, results: {} });
    renderWithProviders(<SystemSection />);

    await userEvent.click(await screen.findByRole('button', { name: /database cleanup/i }));

    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText(/prune snapshots/i), '30');
    await userEvent.click(within(dialog).getByRole('button', { name: /run cleanup/i }));

    expect(api.postCleanup).toHaveBeenCalledWith({
      clear_cache: true,
      prune_snapshots_days: 30,
      remove_inactive_sidecars_days: null,
    });
  });
});
