// Risk model for the home screen: joins /usage/fleet (current gauge state)
// with /usage/forecast (trajectory) and answers "am I about to hit a limit?"
//
// A provider lands in the at-risk rail when its most-restrictive gauge is
// already hot (pct thresholds / error card) OR its forecast projects
// exhaustion before the window resets.

import type { FleetEntry, ForecastEntry, ForecastStatus } from '@/api/types';
import { cardStatus, type QuotaStatus } from '@/lib/quota';

export type RiskLevel = 'critical' | 'warning' | 'ok';

export interface RiskItem {
  key: string; // `${provider_id}:${account_id}`
  entry: FleetEntry;
  status: QuotaStatus; // gauge status (drives colors)
  level: RiskLevel; // combined gauge+forecast severity (drives rail placement)
  forecast: ForecastEntry | null; // worst matching forecast, if any
  score: number; // sort key within a level (higher = more urgent)
}

const FORECAST_SEVERITY: Partial<Record<ForecastStatus, RiskLevel>> = {
  exhausted: 'critical',
  risk: 'critical',
  near_limit: 'warning',
  warn: 'warning',
};

const LEVEL_RANK: Record<RiskLevel, number> = { critical: 2, warning: 1, ok: 0 };

function worstForecast(entry: FleetEntry, forecasts: ForecastEntry[]): ForecastEntry | null {
  const candidates = forecasts.filter(
    (f) =>
      f.provider_id === entry.provider_id &&
      f.account_id === entry.account_id &&
      FORECAST_SEVERITY[f.status] !== undefined,
  );
  if (candidates.length === 0) return null;
  return candidates.reduce((worst, f) => {
    const rank = LEVEL_RANK[FORECAST_SEVERITY[f.status] ?? 'ok'];
    const worstRank = LEVEL_RANK[FORECAST_SEVERITY[worst.status] ?? 'ok'];
    if (rank !== worstRank) return rank > worstRank ? f : worst;
    return (f.projected_pct ?? 0) > (worst.projected_pct ?? 0) ? f : worst;
  });
}

export function buildRiskItems(fleet: FleetEntry[], forecasts: ForecastEntry[]): RiskItem[] {
  return fleet.map((entry) => {
    const status = cardStatus(entry.critical_gauge);
    const forecast = worstForecast(entry, forecasts);

    let level: RiskLevel = 'ok';
    if (status === 'critical') level = 'critical';
    else if (status === 'warning') level = 'warning';
    const forecastLevel = forecast ? (FORECAST_SEVERITY[forecast.status] ?? 'ok') : 'ok';
    if (LEVEL_RANK[forecastLevel] > LEVEL_RANK[level]) level = forecastLevel;

    const pct = entry.critical_gauge.pct_used ?? 0;
    const projected = forecast?.projected_pct ?? 0;
    return {
      key: `${entry.provider_id}:${entry.account_id}`,
      entry,
      status,
      level,
      forecast,
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
