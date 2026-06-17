// Risk model for the home screen: joins /usage/fleet (current gauge state)
// with /usage/forecast (trajectory) and answers "am I about to hit a limit?"
//
// A provider lands in the at-risk rail when its most-restrictive gauge is
// already hot (pct thresholds / error card) OR its forecast projects
// exhaustion before the window resets.

import type { FleetEntry, ForecastEntry, ForecastStatus, LimitCard } from '@/api/types';
import { cardPct, cardStatus, findForecast, type QuotaStatus } from '@/lib/quota';

export type RiskLevel = 'critical' | 'warning' | 'ok';

export interface RiskItem {
  key: string; // `${provider_id}:${account_id}`
  entry: FleetEntry;
  status: QuotaStatus; // gauge status of critical_gauge (drives grid colors)
  level: RiskLevel; // combined gauge+forecast severity (drives rail placement)
  forecast: ForecastEntry | null; // forecast for the lead window
  lead: { card: LimitCard; status: QuotaStatus }; // window to feature on the rail card
  score: number; // sort key within a level (higher = more urgent)
}

const FORECAST_SEVERITY: Partial<Record<ForecastStatus, RiskLevel>> = {
  exhausted: 'critical',
  risk: 'critical',
  near_limit: 'warning',
  warn: 'warning',
};

const LEVEL_RANK: Record<RiskLevel, number> = { critical: 2, warning: 1, ok: 0 };

// Compute the per-account forecasts keyed on (provider_id, account_id) so we
// can look them up without rescanning the full list per-card.
function accountForecasts(
  entry: FleetEntry,
  forecasts: ForecastEntry[],
): ForecastEntry[] {
  return forecasts.filter(
    (f) => f.provider_id === entry.provider_id && f.account_id === entry.account_id,
  );
}

// Combined severity for a single window card + its matched forecast.
function windowLevel(card: LimitCard, forecast: ForecastEntry | null): RiskLevel {
  const gs = cardStatus(card);
  let level: RiskLevel = 'ok';
  if (gs === 'critical') level = 'critical';
  else if (gs === 'warning') level = 'warning';
  const forecastLevel = forecast ? (FORECAST_SEVERITY[forecast.status] ?? 'ok') : 'ok';
  if (LEVEL_RANK[forecastLevel] > LEVEL_RANK[level]) level = forecastLevel;
  return level;
}

export function buildRiskItems(fleet: FleetEntry[], forecasts: ForecastEntry[]): RiskItem[] {
  return fleet.map((entry) => {
    const acctForecasts = accountForecasts(entry, forecasts);
    const allCards = [entry.critical_gauge, ...entry.secondary_limits];

    // Score each window by its combined (gauge + forecast) severity so the
    // rail card shows the window that is actually at risk, not just the window
    // with the highest current %.
    let leadCard = entry.critical_gauge;
    let leadForecast = findForecast(entry.critical_gauge, acctForecasts);
    let leadLevel = windowLevel(leadCard, leadForecast);
    let leadScore = Math.max(cardPct(leadCard) ?? 0, leadForecast?.projected_pct ?? 0);

    for (const card of allCards.slice(1)) {
      const fc = findForecast(card, acctForecasts);
      const lvl = windowLevel(card, fc);
      const sc = Math.max(cardPct(card) ?? 0, fc?.projected_pct ?? 0);
      if (
        LEVEL_RANK[lvl] > LEVEL_RANK[leadLevel] ||
        (LEVEL_RANK[lvl] === LEVEL_RANK[leadLevel] && sc > leadScore)
      ) {
        leadCard = card;
        leadForecast = fc;
        leadLevel = lvl;
        leadScore = sc;
      }
    }

    // Entry-level level/score: max across all windows (same result as before
    // because we just picked the window with the maximum combined severity).
    const level = leadLevel;

    // status stays tied to critical_gauge so the ProviderGrid chip colours
    // (which read item.status) remain a current-state "most exhausted" view.
    const status = cardStatus(entry.critical_gauge);

    const pct = cardPct(entry.critical_gauge) ?? 0;
    const projected = leadForecast?.projected_pct ?? 0;
    return {
      key: `${entry.provider_id}:${entry.account_id}`,
      entry,
      status,
      level,
      forecast: leadForecast,
      lead: { card: leadCard, status: cardStatus(leadCard) },
      score: LEVEL_RANK[level] * 1000 + Math.max(pct, projected),
    };
  });
}

export function atRiskItems(items: RiskItem[]): RiskItem[] {
  return items.filter((i) => i.level !== 'ok').sort((a, b) => b.score - a.score);
}

// Apply the persisted provider_order, appending unknown entries at the end
// in their server order.
export function applyLayoutOrder(items: RiskItem[], order: string[] | undefined): RiskItem[] {
  if (!order || order.length === 0) return items;
  const rank = new Map(order.map((key, i) => [key, i]));
  return [...items].sort((a, b) => {
    const ra = rank.get(a.key) ?? order.length;
    const rb = rank.get(b.key) ?? order.length;
    return ra - rb;
  });
}

// Human label for the forecast badge: "limit in ~2h" / "exhausted".
export function forecastLabel(forecast: ForecastEntry | null): string | null {
  if (!forecast) return null;
  if (forecast.status === 'exhausted') return 'exhausted';
  if (forecast.glide_pct != null && forecast.slope && forecast.slope > 0) {
    const remainingPct = 100 - (forecast.now_pct ?? 0);
    const seconds = remainingPct / forecast.slope;
    if (Number.isFinite(seconds) && seconds > 0) {
      const hours = seconds / 3600;
      if (hours < 1) return `limit in ~${Math.max(1, Math.round(seconds / 60))}m`;
      if (hours < 48) return `limit in ~${Math.round(hours)}h`;
      return `limit in ~${Math.round(hours / 24)}d`;
    }
  }
  if (forecast.projected_pct != null) {
    return `projected ${Math.round(forecast.projected_pct)}%`;
  }
  return forecast.status.replace('_', ' ');
}
