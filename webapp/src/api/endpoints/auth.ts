import { api } from '../client';

// --- Auth -------------------------------------------------------------------

// Exchange the admin key for an HttpOnly session cookie. `remember` selects
// the longer cookie lifetime. Throws ApiError(403) on a bad key.
export const login = (key: string, remember: boolean) =>
  api<{ is_authenticated: boolean }>('/api/v1/auth/session', {
    method: 'POST',
    body: JSON.stringify({ key, remember }),
  });

// Clear this browser's session cookie.
export const logout = () => api<void>('/api/v1/auth/logout', { method: 'POST' });

// Rotate the server session secret — invalidates every session everywhere.
export const revokeAllSessions = () =>
  api<void>('/api/v1/auth/revoke-all', { method: 'POST' });

// --- GitHub OAuth ------------------------------------------------------------

export const initGitHubOAuth = () =>
  api<{
    device_code: string;
    user_code: string;
    verification_uri: string;
    expires_in: number;
    interval: number;
  }>('/api/v1/auth/github/init');

export const pollGitHubOAuth = (deviceCode: string) =>
  api<{ status: string; interval?: number }>('/api/v1/auth/github/poll', {
    method: 'POST',
    body: JSON.stringify({ device_code: deviceCode }),
  });

export const getGitHubOAuthStatus = () =>
  api<{ authenticated: boolean; account?: string; name?: string; email?: string }>(
    '/api/v1/auth/github/status',
  );

export const logoutGitHub = () =>
  api<Record<string, unknown>>('/api/v1/auth/github/logout', { method: 'POST' });
