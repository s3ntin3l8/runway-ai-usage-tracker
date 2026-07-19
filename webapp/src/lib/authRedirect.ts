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
