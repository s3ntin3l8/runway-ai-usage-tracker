// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { BootGate } from './BootGate';
import { fetchAppConfig, fetchSettings, login } from '@/api/endpoints';
import { clearAdminKey, getAdminKey } from '@/api/client';

vi.mock('@/api/endpoints', () => ({
  fetchSettings: vi.fn(),
  fetchAppConfig: vi.fn(),
  login: vi.fn(),
}));

vi.mock('@/api/client', () => ({
  getAdminKey: vi.fn(),
  clearAdminKey: vi.fn(),
}));

const mockSettings = vi.mocked(fetchSettings);
const mockAppConfig = vi.mocked(fetchAppConfig);
const mockLogin = vi.mocked(login);
const mockGetAdminKey = vi.mocked(getAdminKey);
const mockClearAdminKey = vi.mocked(clearAdminKey);

beforeEach(() => {
  vi.clearAllMocks();
  // Default: no legacy key, so the migration effect is a no-op.
  mockGetAdminKey.mockReturnValue(null);
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
    // Submitting with a blank/whitespace key must short-circuit before login().
    await user.click(screen.getByRole('button', { name: /unlock/i }));

    expect(mockLogin).not.toHaveBeenCalled();
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

    // login() sets the cookie; the post-invalidate settings refetch now reports
    // an authenticated session.
    mockLogin.mockResolvedValue({ is_authenticated: true });
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: true,
    } as never);

    await user.type(screen.getByLabelText(/admin key/i), 'super-secret');
    await user.click(screen.getByRole('button', { name: /unlock/i }));

    expect(mockLogin).toHaveBeenCalledWith('super-secret', false);
    expect(await screen.findByText('dashboard-content')).toBeTruthy();
    expect(screen.queryByText(/not accepted/i)).toBeNull();
  });

  it('passes the remember-me choice to login', async () => {
    const user = userEvent.setup();
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: false,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    mockLogin.mockResolvedValue({ is_authenticated: true });
    renderGate();

    await screen.findByText(/requires an admin key/i);
    await user.type(screen.getByLabelText(/admin key/i), 'super-secret');
    await user.click(screen.getByLabelText(/remember me/i));
    await user.click(screen.getByRole('button', { name: /unlock/i }));

    expect(mockLogin).toHaveBeenCalledWith('super-secret', true);
  });

  it('shows an error when the submitted key is rejected', async () => {
    const user = userEvent.setup();
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: false,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();

    await screen.findByText(/requires an admin key/i);

    // login() rejects on a bad key → stays locked with an error.
    mockLogin.mockRejectedValue(new Error('403'));

    await user.type(screen.getByLabelText(/admin key/i), 'wrong-key');
    await user.click(screen.getByRole('button', { name: /unlock/i }));

    expect(mockLogin).toHaveBeenCalledWith('wrong-key', false);
    expect(await screen.findByText(/not accepted/i)).toBeTruthy();
    expect(screen.queryByText('dashboard-content')).toBeNull();
  });

  it('migrates a legacy localStorage key to a session cookie, then clears it', async () => {
    mockGetAdminKey.mockReturnValue('legacy-key');
    mockLogin.mockResolvedValue({ is_authenticated: true });
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: true,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();

    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith('legacy-key', true));
    await waitFor(() => expect(mockClearAdminKey).toHaveBeenCalled());
  });

  it('keeps the legacy key if the migration exchange fails', async () => {
    mockGetAdminKey.mockReturnValue('legacy-key');
    mockLogin.mockRejectedValue(new Error('network'));
    mockSettings.mockResolvedValue({
      admin_auth_required: true,
      is_authenticated: true,
    } as never);
    mockAppConfig.mockResolvedValue({} as never);
    renderGate();

    await waitFor(() => expect(mockLogin).toHaveBeenCalled());
    expect(mockClearAdminKey).not.toHaveBeenCalled();
  });
});
