// Number / currency / token formatters (port of frontend/js/utils/format.js).
//
// Two flavours of "compact number" exist intentionally and are NOT unified:
//   - formatNumber: K/M/B with 1 decimal; preserves integers untouched.
//   - formatTokens: K/M/B with 2 decimals on B and M, 0 decimals on K. Used
//     where tokens are the primary value.

export function formatNumber(num: number | null | undefined): string {
  if (num === null || num === undefined || Number.isNaN(num)) return '—';
  const abs = Math.abs(num);
  if (abs >= 1e9) return `${(num / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(num / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(num / 1e3).toFixed(1)}K`;
  if (Number.isInteger(num)) return num.toString();
  return num.toFixed(1);
}

const formatterCache = new Map<string, Intl.NumberFormat>();

function getCurrencyFormatter(currencyCode?: string | null): Intl.NumberFormat {
  const code = (currencyCode || 'USD').toUpperCase();
  const locale = (typeof navigator !== 'undefined' && navigator.language) || 'en-US';
  const key = `${locale}|${code}`;
  let f = formatterCache.get(key);
  if (!f) {
    try {
      f = new Intl.NumberFormat(locale, {
        style: 'currency',
        currency: code,
        currencyDisplay: 'narrowSymbol',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    } catch {
      f = new Intl.NumberFormat(locale, {
        style: 'currency',
        currency: 'USD',
        currencyDisplay: 'narrowSymbol',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    }
    formatterCache.set(key, f);
  }
  return f;
}

export function formatCurrency(
  amount: number | null | undefined,
  currencyCode?: string | null,
): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return '—';
  return getCurrencyFormatter(currencyCode).format(amount);
}

// Token formatter. Zero renders as literal '0' (not '—') because detail
// panes show explicit zero rows.
export function formatTokens(val: number | null | undefined): string {
  if (val == null || val === 0) return '0';
  if (val >= 1e9) return (val / 1e9).toFixed(2) + 'B';
  if (val >= 1e6) return (val / 1e6).toFixed(2) + 'M';
  if (val >= 1e3) return (val / 1e3).toFixed(0) + 'K';
  return String(val);
}

// Cost formatter. Distinguishes 0 (literal $0.00) from sub-cent (< $0.01)
// from null/missing (—) — all three render differently in cost panes.
export function formatCost(usd: number | null | undefined, currencyCode = 'USD'): string {
  if (usd == null) return '—';
  const f = getCurrencyFormatter(currencyCode);
  if (usd === 0) return f.format(0);
  if (usd > 0 && usd < 0.01) return '< ' + f.format(0.01);
  return f.format(usd);
}

export function formatPct(pct: number | null | undefined, digits = 0): string {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return '—';
  return `${pct.toFixed(digits)}%`;
}

// Compact duration for countdowns: "3d 4h", "4h 12m", "12m", "<1m".
export function formatDuration(ms: number): string {
  if (ms <= 0) return 'now';
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return '<1m';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  return `${mins}m`;
}

// "4h 12m" until an ISO timestamp, or null when missing/past-invalid.
export function timeUntil(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return null;
  return formatDuration(target - Date.now());
}

// Relative "ago" formatter for last-seen / updated-at stamps.
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '—';
  const ms = Date.now() - then;
  if (ms < 60_000) return 'just now';
  return `${formatDuration(ms)} ago`;
}
