// At-a-glance KPI strip for the Overview tab: current quota %, projected at
// reset, spend, burn, tokens and cache-hit — pulled from the fleet card plus
// the cumulative / cost-forecast / forecast endpoints.

import { useMemo } from 'react';
import type { CumulativeBucket, CumulativeModelBucket, FleetEntry } from '@/api/types';
import { StatTile } from '@/components/ui/StatTile';
import { formatCost, formatPct, formatTokens } from '@/lib/format';
import { cardPct, cardStatus, windowLabel } from '@/lib/quota';
import { useProviderCostForecast, useProviderCumulative, useProviderForecast } from './queries';

function sumTokens(b: CumulativeModelBucket | null | undefined, excludeCache = false): number {
  if (!b) return 0;
  return (
    (b.tokens_input ?? 0) +
    (b.tokens_output ?? 0) +
    (excludeCache ? 0 : (b.tokens_cache_read ?? 0) + (b.tokens_cache_create ?? 0)) +
    (b.tokens_reasoning ?? 0)
  );
}

export function ProviderKpis({
  entry,
  excludeCache = false,
}: {
  entry: FleetEntry;
  excludeCache?: boolean;
}) {
  const { provider_id: providerId, account_id: accountId, critical_gauge: critical } = entry;
  const cumulative = useProviderCumulative(providerId, accountId);
  const cost = useProviderCostForecast(providerId, accountId);
  const forecast = useProviderForecast(providerId, accountId);

  const monthBucket = useMemo<CumulativeBucket | null>(() => {
    const data = cumulative.data;
    if (!data) return null;
    const row = data.cumulative.find(
      (c) => c.provider_id === providerId && c.account_id === accountId,
    );
    const bucket = row?.[data.current_month_key];
    return bucket && typeof bucket !== 'string' ? bucket : null;
  }, [cumulative.data, providerId, accountId]);

  // Forecast entry for the gauge we treat as critical (fall back to the first).
  const criticalForecast = useMemo(() => {
    const fs = forecast.data?.forecasts ?? [];
    return fs.find((f) => f.window_type === critical.window_type) ?? fs[0] ?? null;
  }, [forecast.data, critical.window_type]);

  const monthTokens = sumTokens(monthBucket, excludeCache);
  // Cache-hit is inherently a cache metric — always computed against the full
  // total so it stays meaningful regardless of the "Exclude cache" toggle.
  const fullTokens = sumTokens(monthBucket);
  const cacheTokens = (monthBucket?.tokens_cache_read ?? 0) + (monthBucket?.tokens_cache_create ?? 0);
  const cacheHitPct = fullTokens > 0 ? (cacheTokens / fullTokens) * 100 : null;
  const pct = cardPct(critical);

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile
        label="Current"
        value={pct != null ? formatPct(pct) : (critical.remaining ?? '—')}
        hint={windowLabel(critical) ?? critical.window_type}
        status={cardStatus(critical)}
      />
      <StatTile
        label="Projected at reset"
        value={criticalForecast?.projected_pct != null ? formatPct(criticalForecast.projected_pct) : '—'}
        hint={
          criticalForecast?.confidence != null
            ? `${Math.round(criticalForecast.confidence * 100)}% conf`
            : undefined
        }
        loading={forecast.isPending}
      />
      <StatTile
        label="Spend (MTD)"
        value={formatCost(cost.data?.current_month_to_date ?? null)}
        hint={cost.data ? `→ ${formatCost(cost.data.projected_eom)} EOM` : undefined}
        loading={cost.isPending}
      />
      <StatTile
        label="Daily burn (7d)"
        value={formatCost(cost.data?.daily_burn_avg_7d ?? null)}
        hint={cost.data ? `${cost.data.days_remaining}d left` : undefined}
        loading={cost.isPending}
      />
      <StatTile
        label="Tokens (month)"
        value={formatTokens(monthTokens)}
        hint={monthBucket?.msgs != null ? `${monthBucket.msgs} msgs` : undefined}
        loading={cumulative.isPending}
      />
      <StatTile
        label="Cache hit"
        value={cacheHitPct != null ? formatPct(cacheHitPct) : '—'}
        loading={cumulative.isPending}
      />
    </div>
  );
}
