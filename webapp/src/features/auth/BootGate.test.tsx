// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { BootGate } from './BootGate';
import { fetchAppConfig, fetchSettings } from '@/api/endpoints';

vi.mock('@/api/endpoints', () => ({
  fetchSettings: vi.fn(),
  fetchAppConfig: vi.fn(),
}));

const mockSettings = vi.mocked(fetchSettings);
const mockAppConfig = vi.mocked(fetchAppConfig);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderGate(child: ReactNode = <div>dashboard-content</div>) {
  // retry:false so rejected queries surface immediately instead of backing off.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BootGate>{child}</BootGate>
    </QueryClientProvider>,
  );
}

describe('BootGate', () => {
  it('shows backend-unreachable when settings fails to load', async () => {
    mockSettings.mockRejectedValue(new Error('offline'));
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();
    expect(await screen.findByText(/backend unreachable/i)).toBeTruthy();
    expect(screen.queryByText('dashboard-content')).toBeNull();
  });

  it('shows the admin-key screen when the instance is locked', async () => {
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: false,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();
    expect(await screen.findByText(/requires an admin key/i)).toBeTruthy();
    expect(screen.queryByText('dashboard-content')).toBeNull();
  });

  it('renders children once settings resolve and the instance is open', async () => {
    mockSettings.mockResolvedValue({
      admin_auth_required: false,
      is_authenticated: false,
    } as never);
    mockAppConfig.mockResolvedValue({ user_timezone: 'UTC' } as never);
    renderGate();
    expect(await screen.findByText('dashboard-content')).toBeTruthy();
  });

  it('renders children when an authenticated user has admin auth on', async () => {
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: true,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();
    await waitFor(() => expect(screen.getByText('dashboard-content')).toBeTruthy());
  });
});
