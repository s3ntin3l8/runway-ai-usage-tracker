// Shared quota clustering + status utilities (port of frontend/js/utils/quota.js).
//
// Clustering uses an explicit collector-set `quota_pool_id` rather than
// behavioral similarity. Cards with matching non-null pool_id share a
// physical quota; everything else stands alone. This avoids the
// Gemini-Flash + Gemini-Flash-Lite false-positive trap where two
// independent daily quotas at the same percentage and reset time got
// lumped into a fake "SHARED" pool.

import type { ForecastEntry, LimitCard, TokenUsage } from '@/api/types';

export function sameQuota(a: LimitCard, b: LimitCard): boolean {
  return a.quota_pool_id != null && a.quota_pool_id === b.quota_pool_id;
}

export function clusterPools(cards: LimitCard[]): LimitCard[][] {
  const clusters: LimitCard[][] = [];
  const seen = new Set<LimitCard>();
  for (const card of cards) {
    if (seen.has(card)) continue;
    const cluster = [card];
    seen.add(card);
    for (const other of cards) {
      if (seen.has(other)) continue;
      if (sameQuota(card, other)) {
        cluster.push(other);
        seen.add(other);
      }
    }
    clusters.push(cluster);
  }
  return clusters;
}

// Compact display label listing a cluster's model names. Strips "AG: "
// provider prefix noise; truncates to "+N" beyond 2 names.
export function clusterModelLabel(cluster: LimitCard[]): string {
  const names = cluster
    .map((c) => (c.service_name || c.model_id || '').replace(/^AG:\s*/i, ''))
    .filter(Boolean);
  if (names.length <= 2) return names.join(' · ');
  return names.slice(0, 2).join(' · ') + ` +${names.length - 2}`;
}

// --- Status semantics ------------------------------------------------------

export type QuotaStatus = 'critical' | 'warning' | 'ok' | 'unlimited' | 'unknown';

export const CRITICAL_PCT = 90;
export const WARNING_PCT = 70;

export function statusForPct(pct: number | null | undefined): QuotaStatus {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return 'unknown';
  if (pct >= CRITICAL_PCT) return 'critical';
  if (pct >= WARNING_PCT) return 'warning';
  return 'ok';
}

// Effective percentage: pct_used when the collector provided one, otherwise
// derived from used/limit (many quota cards ship only the raw pair).
export function cardPct(card: LimitCard): number | null {
  if (card.pct_used !== null && card.pct_used !== undefined) return card.pct_used;
  if (
    card.used_value !== null &&
    card.used_value !== undefined &&
    card.limit_value !== null &&
    card.limit_value !== undefined &&
    card.limit_value > 0
  ) {
    return (card.used_value / card.limit_value) * 100;
  }
  return null;
}

// Card → semantic status token. Precedence: error cards > unlimited >
// collector-asserted health > percentage thresholds.
// Stale cards (collection failing) skip the health override — the real
// quota percentage drives status so old-but-valid data doesn't trigger
// false at-risk alerts for occasionally-used providers.
export function cardStatus(card: LimitCard): QuotaStatus {
  if (card.error_type) return 'critical';
  if (card.is_unlimited) return 'unlimited';
  if (!card.stale && card.health === 'critical') return 'critical';
  if (card.health === 'warning') return 'warning';
  return statusForPct(cardPct(card));
}

// --- Card kind classification -------------------------------------------

export type CardKind = 'quota' | 'tokens' | 'spend';

// Classify a card by what its hero metric should be:
//   quota  — a percentage (limit_value present, or pct_used derivable)
//   tokens — an absolute token count (is_unlimited, or token unit, no derivable %)
//   spend  — a currency amount (currency-typed, no fixed limit)
// Falls back to 'quota' so unclassifiable cards degrade to the existing '—' gauge.
export function cardKind(card: LimitCard): CardKind {
  if (cardPct(card) != null) return 'quota';
  const ut = card.unit_type;
  if (ut === 'currency' || ut === 'credits' || card.currency) return 'spend';
  if (card.is_unlimited || ut === 'token' || ut === 'tokens' || card.token_usage)
    return 'tokens';
  return 'quota';
}

// Lifetime token total from a card's per-component token_usage, respecting the
// exclude-cache toggle (unlike the pre-aggregated token_usage.total, which always
// excludes cache). Returns null only when token_usage itself is absent — callers
// chain with `??` for their own fallback (e.g. used_value, or a cumulative-bucket
// sum), since a present-but-zero total is a real value, not a missing one.
export function tokenUsageTotal(
  tu: TokenUsage | null | undefined,
  excludeCache = false,
): number | null {
  if (!tu) return null;
  return (
    (tu.input ?? 0) +
    (tu.output ?? 0) +
    (tu.reasoning ?? 0) +
    (excludeCache ? 0 : (tu.cache_read ?? 0) + (tu.cache_create ?? 0))
  );
}

// "weekly" → "Weekly", "session" → "Session"; null for unknown windows.
export function windowLabel(card: LimitCard): string | null {
  const w = card.window_type;
  if (!w || w === 'unknown') return null;
  return w.charAt(0).toUpperCase() + w.slice(1);
}

// Humanize a model id for display: de-separate and capitalize each word
// ("sonnet" → "Sonnet", "gemini-flash" → "Gemini Flash").
export function modelLabel(modelId: string): string {
  return modelId
    .split(/[-_/]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

// Match a quota card to its forecast entry from a flat forecasts array.
// Prefer an exact (window_type, variant, model_id) match; fall back to
// window_type alone. Returns null when no matching forecast exists.
// This is the canonical resolver used by the home rail and the provider
// detail panel — both must resolve forecasts the same way.
export function findForecast(card: LimitCard, forecasts: ForecastEntry[]): ForecastEntry | null {
  return (
    forecasts.find(
      (f) =>
        f.window_type === card.window_type &&
        (f.variant ?? null) === (card.variant ?? null) &&
        (f.model_id ?? null) === (card.model_id ?? null),
    ) ??
    forecasts.find((f) => f.window_type === card.window_type) ??
    null
  );
}

// Build the chip label for a card *as if* it had no last-resort name
// safeguard. Used internally to compare what siblings would render, so the
// safeguard only fires when a sibling truly collapses to the same string.
function chipBaseLabel(card: LimitCard, sameWinSiblings: LimitCard[]): string {
  const name = card.service_name || card.model_id || '';
  const win = windowLabel(card);
  const base = win || name || '?';
  if (
    card.model_id &&
    name !== card.model_id &&
    base.toLowerCase() !== card.model_id.toLowerCase() &&
    sameWinSiblings.some((s) => !s.model_id || s.model_id !== card.model_id)
  ) {
    return `${base} · ${modelLabel(card.model_id)}`;
  }
  if (card.variant && sameWinSiblings.some((s) => s.variant !== card.variant)) {
    const variantLabel = card.variant.charAt(0).toUpperCase() + card.variant.slice(1);
    return `${base} · ${variantLabel}`;
  }
  return base;
}

// Label for a secondary-limit chip. Always prefers the window as the base
// ("Session" / "Weekly" / "Monthly") so the badge doesn't repeat the provider
// name that's already in the card header. When a window is model-scoped (e.g.
// Claude's Sonnet-specific weekly alongside the generic weekly) the base
// would still collapse to "Weekly" for both, so append the model. When
// multiple siblings share the same window_type (e.g. Antigravity's gemini
// session vs frontier session) append the variant to disambiguate. As a
// last-resort safeguard, if after model/variant suffixing a sibling would
// still render the same bare label, append the service_name so the two
// badges don't silently collapse.
export function chipLabel(card: LimitCard, siblings: LimitCard[]): string {
  const name = card.service_name || card.model_id || '';
  const sameWinSiblings = siblings.filter((s) => s.window_type === card.window_type);
  const base = chipBaseLabel(card, sameWinSiblings);
  // Last-resort safeguard: a sibling shares the window_type AND has a
  // different service_name AND would, after all model/variant suffixing,
  // still render identically to this card. No current collector hits this
  // path — the collapse would be silent and confusing.
  if (
    name &&
    base !== name &&
    base !== '?' &&
    sameWinSiblings.some(
      (s) => s.service_name !== name && chipBaseLabel(s, sameWinSiblings) === base,
    )
  ) {
    return `${base} · ${name}`;
  }
  return base;
}
