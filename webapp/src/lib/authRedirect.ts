import { ApiError } from '@/api/client';

// After this many consecutive page loads where the global auth-redirect guard
// fires (i.e. the reload didn't land on the SSO login page), stop
// auto-reloading and let BootGate's static "Session expired" card handle it.
// This breaks the infinite reload loop on Android PWA standalone, where
// window.location.reload() with a cross-origin SSO redirect opens a Custom
// Chrome Tab instead of navigating within the standalone window.
const AUTH_RELOAD_KEY = 'runway_auth_reload_count';
const AUTH_RELOAD_MAX = 2;

// A lost upstream SSO session (see api/client.ts's authRedirect handling) can
// surface from *any* query or mutation, not just BootGate's boot-time
// settings probe — e.g. the session expires while the dashboard is already
// open. Every affected request settles into an authRedirect error at roughly
// the same moment, so this guards the reaction to fire once per page load.
export function createAuthRedirectGuard(onExpire: () => void): (error: unknown) => void {
  let handled = false;
  return (error: unknown) => {
    if (handled || !(error instanceof ApiError) || !error.authRedirect) return;
    handled = true;
    const count = Number(sessionStorage.getItem(AUTH_RELOAD_KEY)) || 0;
    if (count >= AUTH_RELOAD_MAX) {
      // We've reloaded AUTH_RELOAD_MAX times without reaching the SSO login
      // page — further reloads won't help (likely Android PWA standalone where
      // the cross-origin redirect can't navigate within the app window). Stop
      // reloading and let BootGate show its "Session expired" card instead.
      return;
    }
    sessionStorage.setItem(AUTH_RELOAD_KEY, String(count + 1));
    onExpire();
  };
}

// Call on successful boot to clear the reload counter — the SSO session is
// (still) valid, so any previous reload attempts are now stale. Also called
// when BootGate's "Session expired" card is shown, so the user's manual
// "Sign in" click gets a fresh reload budget.
export function clearAuthReloadCount(): void {
  sessionStorage.removeItem(AUTH_RELOAD_KEY);
}

// Set as `meta: { skipAuthRedirectGuard: true }` on BootGate's boot-time
// settings query so its authRedirect failures are excluded from the guard
// above. That query already owns its own "Session expired — Sign in" UI; if
// it also fed the global guard, a cold boot with an expired session would
// both flash that card AND trigger the guard's auto-reload ~1.5s later,
// racing the card's own "Sign in" button and making it effectively dead.
export const SKIP_AUTH_REDIRECT_GUARD_META = { skipAuthRedirectGuard: true } as const;

interface QueryLike {
  meta?: Record<string, unknown> | null;
}

// Wraps a QueryCache's onError so a query flagged with the meta above is
// excluded from the shared guard, while every other query still feeds it.
export function createQueryErrorAuthRedirectHandler(
  guard: (error: unknown) => void,
): (error: unknown, query: QueryLike) => void {
  return (error, query) => {
    if (query.meta?.skipAuthRedirectGuard) return;
    guard(error);
  };
}
