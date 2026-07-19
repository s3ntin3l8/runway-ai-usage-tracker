import { ApiError } from '@/api/client';

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
    onExpire();
  };
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
