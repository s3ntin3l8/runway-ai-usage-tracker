import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { ClaudeTelemetrySection } from './ClaudeTelemetrySection';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const sidecar = (o: Partial<Sidecar> = {}): Sidecar => ({
  sidecar_id: 'laptop',
  hostname: 'laptop',
  claude_telemetry: 'disabled',
  claude_telemetry_desired: null,
  effective_claude_telemetry: false,
  ...o,
});

describe('ClaudeTelemetrySection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchAppConfig).mockResolvedValue({
      sidecar_claude_telemetry_default: false,
    });
  });

  it('renders description mentioning settings.json and new sessions and saves override', async () => {
    vi.mocked(api.setSidecarSettings).mockResolvedValue({
      status: 'ok',
      auto_update_desired: null,
      update_channel_desired: null,
      log_level_desired: null,
      claude_telemetry_desired: true,
      effective_auto_update: false,
      effective_update_channel: 'stable',
      effective_claude_telemetry: true,
    });
    renderWithProviders(<ClaudeTelemetrySection sidecar={sidecar()} online />);

    expect(screen.getByText(/~\/\.claude\/settings\.json/)).toBeInTheDocument();
    expect(screen.getByText(/only sessions started afterwards are covered/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('combobox', { name: /claude code telemetry/i }));
    await userEvent.click(await screen.findByRole('option', { name: /always on/i }));

    expect(api.setSidecarSettings).toHaveBeenCalledWith('laptop', { claude_telemetry: true });
  });

  it('shows a conflict callout when the sidecar reports conflict', async () => {
    renderWithProviders(
      <ClaudeTelemetrySection
        sidecar={sidecar({ claude_telemetry: 'conflict', effective_claude_telemetry: true })}
        online
      />,
    );
    expect(await screen.findByTestId('claude-telemetry-conflict')).toBeInTheDocument();
  });

  it('shows an error callout when the sidecar reports error', async () => {
    renderWithProviders(
      <ClaudeTelemetrySection
        sidecar={sidecar({ claude_telemetry: 'error', effective_claude_telemetry: true })}
        online
      />,
    );
    expect(await screen.findByTestId('claude-telemetry-error')).toBeInTheDocument();
  });
});
