// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { BootGate } from './BootGate';
import { fetchAppConfig, fetchSettings } from '@/api/endpoints';
import { setAdminKey } from '@/api/client';

vi.mock('@/api/endpoints', () => ({
  fetchSettings: vi.fn(),
  fetchAppConfig: vi.fn(),
}));

vi.mock('@/api/client', () => ({
  setAdminKey: vi.fn(),
}));

const mockSettings = vi.mocked(fetchSettings);
const mockAppConfig = vi.mocked(fetchAppConfig);
const mockSetAdminKey = vi.mocked(setAdminKey);

beforeEach(() => {
  vi.clearAllMocks();
});

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

  it('shows the loading mark while settings are still pending', () => {
    // Never-resolving promises keep both queries in the pending branch.
    mockSettings.mockReturnValue(new Promise(() => {}) as never);
    mockAppConfig.mockReturnValue(new Promise(() => {}) as never);
    const { container } = renderGate();
    expect(screen.queryByText('dashboard-content')).toBeNull();
    expect(screen.queryByText(/backend unreachable/i)).toBeNull();
    // The pulse mark is the only thing rendered during boot.
    expect(container.querySelector('.animate-pulse')).toBeTruthy();
  });

  it('refetches settings when the retry button is clicked', async () => {
    const user = userEvent.setup();
    mockSettings.mockRejectedValueOnce(new Error('offline'));
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();

    expect(await screen.findByText(/backend unreachable/i)).toBeTruthy();
    expect(mockSettings).toHaveBeenCalledTimes(1);

    // The retry now succeeds with an open instance, so children should appear.
    mockSettings.mockResolvedValue({
      admin_auth_required: false,
      is_authenticated: false,
    } as never);
    await user.click(screen.getByRole('button', { name: /retry/i }));

    expect(await screen.findByText('dashboard-content')).toBeTruthy();
    expect(mockSettings.mock.calls.length).toBeGreaterThan(1);
  });

  it('ignores an empty-key submit on the auth screen', async () => {
    const user = userEvent.setup();
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: false,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();

    await screen.findByText(/requires an admin key/i);
    const callsBefore = mockSettings.mock.calls.length;
    // Submitting with a blank/whitespace key must short-circuit (line 64).
    await user.click(screen.getByRole('button', { name: /unlock/i }));

    expect(mockSetAdminKey).not.toHaveBeenCalled();
    expect(mockSettings.mock.calls.length).toBe(callsBefore);
    expect(screen.queryByText(/not accepted/i)).toBeNull();
  });

  it('unlocks the app when a valid admin key is submitted', async () => {
    const user = userEvent.setup();
    mockSettings.mockResolvedValueOnce({
      admin_auth_required: true,
      is_authenticated: false,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();

    await screen.findByText(/requires an admin key/i);

    // After the key is set, re-fetching settings reports an authenticated session.
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: true,
    } as never);

    await user.type(screen.getByLabelText(/admin key/i), 'super-secret');
    await user.click(screen.getByRole('button', { name: /unlock/i }));

    expect(mockSetAdminKey).toHaveBeenCalledWith('super-secret');
    expect(await screen.findByText('dashboard-content')).toBeTruthy();
    expect(screen.queryByText(/not accepted/i)).toBeNull();
  });

  it('shows an error when the submitted key is rejected', async () => {
    const user = userEvent.setup();
    mockSettings.mockResolvedValueOnce({
      admin_auth_required: true,
      is_authenticated: false,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();

    await screen.findByText(/requires an admin key/i);

    // The post-submit fetch rejects → null → stays locked with an error.
    mockSettings.mockRejectedValue(new Error('401'));

    await user.type(screen.getByLabelText(/admin key/i), 'wrong-key');
    await user.click(screen.getByRole('button', { name: /unlock/i }));

    expect(mockSetAdminKey).toHaveBeenCalledWith('wrong-key');
    expect(await screen.findByText(/not accepted/i)).toBeTruthy();
    expect(screen.queryByText('dashboard-content')).toBeNull();
  });
});
