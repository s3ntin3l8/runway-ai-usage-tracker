// Boot sequence: load /system/settings (auth state) and /system/app-config
// (timezone) before rendering the app, so charts never flash UTC and locked
// deployments land on the key screen instead of a wall of 401s.

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { fetchAppConfig, fetchSettings, login } from '@/api/endpoints';
import { ApiError, clearAdminKey, getAdminKey } from '@/api/client';
import { setTzConfig } from '@/lib/tz';
import { SKIP_AUTH_REDIRECT_GUARD_META } from '@/lib/authRedirect';
import { RunwayMark } from '@/components/layout/RunwayMark';

export function BootGate({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ['system', 'settings'],
    queryFn: fetchSettings,
    // A backend blip (an :edge redeploy → brief 5xx/network) shouldn't strand the
    // user on the error card. Once the global retry budget is spent, keep polling
    // while errored so the app self-heals when the server is back — no manual refresh.
    refetchInterval: (query) => (query.state.status === 'error' ? 3000 : false),
    // This query owns its own "Session expired" UI below on an authRedirect —
    // exclude it from app.tsx's global auto-reload guard so a cold boot
    // doesn't also toast-and-reload out from under this screen's own button.
    meta: SKIP_AUTH_REDIRECT_GUARD_META,
  });
  const appConfig = useQuery({
    queryKey: ['system', 'app-config'],
    queryFn: fetchAppConfig,
    // tz is nice-to-have; don't block a locked instance on it
    retry: false,
  });

  useEffect(() => {
    if (appConfig.data) setTzConfig(appConfig.data);
  }, [appConfig.data]);

  // One-time migration: trade a legacy localStorage admin key for a session
  // cookie, then drop it from localStorage (XSS hardening). Non-blocking — if
  // the exchange fails the key stays put and the header fallback still works.
  useEffect(() => {
    const legacy = getAdminKey();
    if (!legacy) return;
    login(legacy, true)
      .then(() => {
        clearAdminKey();
        void queryClient.invalidateQueries();
      })
      .catch(() => {
        /* keep the legacy key; X-Admin-Key header still authenticates */
      });
  }, [queryClient]);

  if (settings.isPending || appConfig.isPending) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <RunwayMark className="size-8 animate-pulse" />
      </div>
    );
  }

  if (settings.isError) {
    // An upstream forward-auth proxy (Authentik/oauth2-proxy/Authelia in front
    // of Runway) bounced the settings probe to its login page — the session
    // with *that* proxy is gone, not the Runway backend. Reloading the page is
    // a top-level navigation, so the browser (or the service worker's
    // network-first navigation route) follows the redirect to the real login
    // screen instead of us faking one here.
    if (settings.error instanceof ApiError && settings.error.authRedirect) {
      return (
        <CenteredCard>
          <h1 className="text-base font-semibold">Session expired</h1>
          <p className="mt-1 text-sm text-fg-muted">
            Your login with the upstream auth provider has expired. Sign in again to continue.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-4 h-9 cursor-pointer rounded-sm bg-accent px-4 text-sm font-medium text-accent-fg transition-colors duration-150 hover:bg-accent-hover"
          >
            Sign in
          </button>
        </CenteredCard>
      );
    }

    return (
      <CenteredCard>
        <h1 className="text-base font-semibold">Backend unreachable</h1>
        <p className="mt-1 text-sm text-fg-muted">
          Could not reach the Runway server. Check that it is running, then retry.
        </p>
        <button
          type="button"
          onClick={() => settings.refetch()}
          className="mt-4 h-9 cursor-pointer rounded-sm bg-accent px-4 text-sm font-medium text-accent-fg transition-colors duration-150 hover:bg-accent-hover"
        >
          Retry
        </button>
      </CenteredCard>
    );
  }

  const locked = settings.data.admin_auth_required && !settings.data.is_authenticated;
  if (locked) return <AuthScreen />;

  return <>{children}</>;
}

function AuthScreen() {
  const queryClient = useQueryClient();
  const [key, setKey] = useState('');
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!key.trim()) return;
    try {
      const res = await login(key.trim(), remember);
      if (res.is_authenticated) {
        setError(null);
        await queryClient.invalidateQueries();
        return;
      }
    } catch {
      // fall through to the error message below
    }
    setError('That key was not accepted.');
  };

  return (
    <CenteredCard>
      <div className="flex items-center gap-2.5">
        <RunwayMark className="size-7" />
        <h1 className="text-base font-semibold tracking-tight">Runway</h1>
      </div>
      <p className="mt-3 text-sm text-fg-muted">
        This instance requires an admin key to continue.
      </p>
      <form onSubmit={submit} className="mt-4 flex flex-col gap-3">
        <label htmlFor="admin-key" className="text-xs font-medium text-fg-muted">
          Admin key
        </label>
        <input
          id="admin-key"
          type="password"
          autoComplete="current-password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          className="h-10 rounded-sm border border-edge bg-surface-2 px-3 text-sm outline-none focus:border-accent"
        />
        {error ? <p className="text-xs text-critical">{error}</p> : null}
        <label className="flex cursor-pointer items-center gap-2 text-xs text-fg-muted">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            className="size-3.5 cursor-pointer accent-accent"
          />
          Remember me on this device
        </label>
        <button
          type="submit"
          className="h-10 cursor-pointer rounded-sm bg-accent text-sm font-medium text-accent-fg transition-colors duration-150 hover:bg-accent-hover"
        >
          Unlock
        </button>
      </form>
    </CenteredCard>
  );
}

function CenteredCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-lg border border-edge bg-surface-1 p-6">
        {children}
      </div>
    </div>
  );
}
