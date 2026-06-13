// Timezone resolution + formatting helpers (port of frontend/js/utils/tz.js).
//
// Resolution chain (highest priority first):
//   1. SystemConfig.user_timezone — explicit dashboard-side override
//   2. backend TZ env var          — deployment-level default (Docker, etc.)
//   3. browser auto-detect         — Intl.DateTimeFormat()
//
// The first two come from /api/v1/system/app-config, fetched at boot (the v1
// UI received them via an inline <script> injection; fetching instead lets
// the server drop 'unsafe-inline' from script-src).

interface TzConfig {
  user_timezone?: string | null;
  env_timezone?: string | null;
}

let config: TzConfig = {};
let cached: string | null = null;

export function setTzConfig(cfg: TzConfig): void {
  config = { ...config, ...cfg };
  cached = null;
}

export function getUserTz(): string {
  if (cached) return cached;
  cached =
    config.user_timezone ||
    config.env_timezone ||
    Intl.DateTimeFormat().resolvedOptions().timeZone ||
    'UTC';
  return cached;
}

// Format helpers — always pass `timeZone` so the result is deterministic
// regardless of how the browser parses the input string.

export function formatLocalTime(
  iso: string | null | undefined,
  opts: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' },
): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString([], { ...opts, timeZone: getUserTz() });
}

export function formatLocalDateTime(
  iso: string | null | undefined,
  opts: Intl.DateTimeFormatOptions = {},
): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString([], { timeZone: getUserTz(), ...opts });
}

export function formatLocalDate(
  iso: string | null | undefined,
  opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' },
): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString([], { ...opts, timeZone: getUserTz() });
}

// UTC ISO instant for the 1st of the current month at 00:00 *in the resolved
// user tz* — the lower bound for "current month" scoped queries. Computed via
// the toLocaleString offset round-trip so it stays correct across DST and tz
// changes (the backend is sensitive to month-boundary tz skew).
export function startOfCurrentMonthISO(): string {
  const tz = getUserTz();
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year: 'numeric',
    month: 'numeric',
  }).formatToParts(now);
  const year = Number(parts.find((p) => p.type === 'year')?.value);
  const month = Number(parts.find((p) => p.type === 'month')?.value); // 1-based
  const utcGuess = Date.UTC(year, month - 1, 1, 0, 0, 0);
  const offset =
    new Date(new Date(utcGuess).toLocaleString('en-US', { timeZone: 'UTC' })).getTime() -
    new Date(new Date(utcGuess).toLocaleString('en-US', { timeZone: tz })).getTime();
  return new Date(utcGuess + offset).toISOString();
}

// Format a Unix epoch (seconds) the same way — used by chart bucket labels
// where the source isn't an ISO string.
export function formatLocalFromEpoch(
  epochSeconds: number,
  opts: Intl.DateTimeFormatOptions = {},
): string {
  return formatLocalDateTime(new Date(epochSeconds * 1000).toISOString(), opts);
}
