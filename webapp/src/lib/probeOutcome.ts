// How a live source probe (`POST /system/debug/sources/…`) outcome reads, shared by the
// provider Debug tab and the Credentials page.

export const PROBE_LABEL: Record<string, string> = {
  healthy: 'Working',
  degraded: 'Working, some requests rejected',
  auth_failed: 'Rejected by the provider',
  unavailable: 'Collection failed',
  waiting_on_machine: "Expired — waiting for its machine's CLI to renew it",
  disabled: 'Disabled — not tried',
  pending: 'Waiting for an account — not tried',
  over_limit: 'Not tried — probe limit reached',
};

export const PROBE_VARIANT: Record<string, 'ok' | 'warning' | 'critical' | 'neutral'> = {
  healthy: 'ok',
  degraded: 'warning',
  auth_failed: 'critical',
  unavailable: 'critical',
};
