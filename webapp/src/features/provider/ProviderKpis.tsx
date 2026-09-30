// At-a-glance KPI strip for the Overview tab: tiles adapt to card kind —
// quota providers show pct + projected-at-reset; token providers show recorded
// usage for the selected range; spend providers show MTD + projected EOM.
// Recorded-usage tiles follow the shared time-range picker; quota windows,
// forward projections, and explicitly lifetime values retain their own scope.
// The forward-looking MTD/EOM/burn tiles only price in the live calendar
// month — any other range falls back to recorded spend (CostTab's pattern).

import { useMemo } from 'react';
import type { CumulativeBucket, FleetEntry } from '@/api/types';
import { StatTile } from '@/components/ui/StatTile';
import { sumTokens } from '@/lib/cumulative';
import { formatCost, formatNumber, formatPct, formatTokens } from '@/lib/format';
import { cardKind, cardPct, cardStatus, findForecast, windowLabel } from '@/lib/quota';
import type { TabScope } from './period';
import {
  useProviderCostForecast,
  useProviderCumulative,
  useProviderCumulativeMonth,
  useProviderCumulativeRange,
  useProviderForecast,
} from './queries';

export function ProviderKpis({
  entry,
  scope,
  excludeCache = false,
}: {
  entry: FleetEntry;
  scope: TabScope;
  excludeCache?: boolean;
}) {
  const { provider_id: providerId, account_id: accountId, critical_gauge: critical } = entry;
  const scopeLabel = scope.label;
  const isLiveMonth = scope.isLiveMonth;
  const isRange = !scope.isLiveMonth && !scope.periodKey;
  // Scope-matching bucket source: live month → live cumulative, a full past
  // month → the month path, anything else → the range path. All three point
  // `current_month_key` at the bucket that holds the data. The live response
  // is fetched regardless — it's the only one carrying the lifetime bucket.
  const liveCumulative = useProviderCumulative(providerId, accountId);
  const monthCumulative = useProviderCumulativeMonth(
    providerId,
    accountId,
    scope.periodKey ?? '',
    !!scope.periodKey && !scope.isLiveMonth,
  );
  const rangeCumulative = useProviderCumulativeRange(providerId, accountId, scope.range, isRange);
  const cumulative = isLiveMonth
    ? liveCumulative
    : scope.periodKey
      ? monthCumulative
      : rangeCumulative;
  const cost = useProviderCostForecast(providerId, accountId);
  const forecast = useProviderForecast(providerId, accountId);
  const billingType = entry.billing_type ?? 'unknown';
  const moneyLabel = billingType === 'pay_as_you_go'
    ? 'Spend'
    : billingType === 'unknown'
      ? 'Usage value'
      : 'Estimated usage value';

  const kind = cardKind(critical);

  const bucket = useMemo<CumulativeBucket | null>(() => {
    const data = cumulative.data;
    if (!data) return null;
    const row = data.cumulative.find(
      (c) => c.provider_id === providerId && c.account_id === accountId,
    );
    const b = row?.[data.current_month_key];
    return b && typeof b !== 'string' ? b : null;
  }, [cumulative.data, providerId, accountId]);

  // Lifetime bucket — used by token/spend kinds for total and lifetime-spend tiles.
  const lifetime = useMemo<CumulativeBucket | null>(() => {
    const row = liveCumulative.data?.cumulative.find(
      (c) => c.provider_id === providerId && c.account_id === accountId,
    );
    return row?.lifetime ?? null;
  }, [liveCumulative.data, providerId, accountId]);

  // Forecast entry for the gauge we treat as critical. Match the full card
  // identity via findForecast (window_type + variant + model_id), not
  // window_type alone — multi-pool providers (e.g. Antigravity gemini/frontier)
  // would otherwise resolve to the empty pool's insufficient-data forecast.
  const criticalForecast = useMemo(() => {
    const fs = forecast.data?.forecasts ?? [];
    return findForecast(critical, fs);
  }, [forecast.data, critical]);

  const bucketLoading = cumulative.isPending;
  const lifetimeLoading = liveCumulative.isPending;
  const scopedTokens = sumTokens(bucket, excludeCache);
  // Cache-hit is inherently a cache metric — always computed against the full
  // total so it stays meaningful regardless of the "Exclude cache" toggle.
  const fullTokens = sumTokens(bucket);
  const cacheTokens = (bucket?.tokens_cache_read ?? 0) + (bucket?.tokens_cache_create ?? 0);
  const cacheHitPct = fullTokens > 0 ? (cacheTokens / fullTokens) * 100 : null;
  const pct = cardPct(critical);

  // Recorded spend for the selected range (non-live-month tiles).
  const rangeSpend = (
    <StatTile
      label={`${moneyLabel} · ${scopeLabel}`}
      value={formatCost(bucket?.cost_usd ?? null)}
      loading={bucketLoading}
    />
  );
  const projectionPlaceholder = (label: string) => (
    <StatTile label={label} value="—" hint="current month only" />
  );

  // --- Quota: current %, projected, spend, burn, tokens, cache-hit ---
  if (kind === 'quota') {
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
        {isLiveMonth ? (
          <StatTile
            label={`${moneyLabel} (MTD)`}
            value={formatCost(cost.data?.current_month_to_date ?? null)}
            hint={cost.data ? `→ ${formatCost(cost.data.projected_eom)} EOM` : undefined}
            loading={cost.isPending}
          />
        ) : (
          rangeSpend
        )}
        {isLiveMonth ? (
          <StatTile
            label={billingType === 'pay_as_you_go' ? 'Daily burn (7d)' : 'Daily usage value (7d)'}
            value={formatCost(cost.data?.daily_burn_avg_7d ?? null)}
            hint={cost.data ? `${cost.data.days_remaining}d left` : undefined}
            loading={cost.isPending}
          />
        ) : (
          projectionPlaceholder(
            billingType === 'pay_as_you_go' ? 'Daily burn (7d)' : 'Daily usage value (7d)',
          )
        )}
        <StatTile
          label={`Tokens · ${scopeLabel}`}
          value={formatTokens(scopedTokens)}
          hint={bucket?.msgs != null ? `${bucket.msgs} msgs` : undefined}
          loading={bucketLoading}
        />
        <StatTile
          label={`Cache hit · ${scopeLabel}`}
          value={cacheHitPct != null ? formatPct(cacheHitPct) : '—'}
          loading={bucketLoading}
        />
      </div>
    );
  }

  // --- Tokens: every recorded-usage tile follows the selected scope. ---
  if (kind === 'tokens') {
    // Show spend tiles only when we actually have cost data in the selected
    // range (e.g. opencode API on free tier that also has cost); otherwise show
    // per-component token counts.
    const hasCost = (bucket?.cost_usd ?? 0) > 0;
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <StatTile
          label="Messages"
          value={bucket?.msgs != null ? formatNumber(bucket.msgs) : '—'}
          hint={scopeLabel}
          loading={bucketLoading}
        />
        <StatTile
          label={`Input · ${scopeLabel}`}
          value={formatTokens(bucket?.tokens_input ?? null)}
          loading={bucketLoading}
        />
        {hasCost ? (
          isLiveMonth ? (
            <StatTile
              label={`${moneyLabel} (MTD)`}
              value={formatCost(cost.data?.current_month_to_date ?? null)}
              hint={cost.data ? `→ ${formatCost(cost.data.projected_eom)} EOM` : undefined}
              loading={cost.isPending}
            />
          ) : (
            rangeSpend
          )
        ) : (
          <StatTile
            label={`Output · ${scopeLabel}`}
            value={formatTokens(bucket?.tokens_output ?? null)}
            loading={bucketLoading}
          />
        )}
        {hasCost ? (
          isLiveMonth ? (
            <StatTile
              label={billingType === 'pay_as_you_go' ? 'Daily burn (7d)' : 'Daily usage value (7d)'}
              value={formatCost(cost.data?.daily_burn_avg_7d ?? null)}
              hint={cost.data ? `${cost.data.days_remaining}d left` : undefined}
              loading={cost.isPending}
            />
          ) : (
            projectionPlaceholder(
              billingType === 'pay_as_you_go' ? 'Daily burn (7d)' : 'Daily usage value (7d)',
            )
          )
        ) : (
          <StatTile
            label={`Cache tokens · ${scopeLabel}`}
            value={formatTokens(cacheTokens)}
            loading={bucketLoading}
          />
        )}
        <StatTile
          label={`Tokens · ${scopeLabel}`}
          value={formatTokens(scopedTokens)}
          hint={bucket?.msgs != null ? `${bucket.msgs} msgs` : undefined}
          loading={bucketLoading}
        />
        <StatTile
          label={`Cache hit · ${scopeLabel}`}
          value={cacheHitPct != null ? formatPct(cacheHitPct) : '—'}
          loading={bucketLoading}
        />
      </div>
    );
  }

  // --- Spend: MTD + projected EOM + burn + scoped tokens + cache + lifetime ---
  const lifetimeCost = lifetime?.cost_usd ?? null;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      {isLiveMonth ? (
        <StatTile
          label={`${moneyLabel} (MTD)`}
          value={formatCost(cost.data?.current_month_to_date ?? null)}
          hint={cost.data ? `→ ${formatCost(cost.data.projected_eom)} EOM` : undefined}
          loading={cost.isPending}
        />
      ) : (
        rangeSpend
      )}
      {isLiveMonth ? (
        <StatTile
          label={billingType === 'pay_as_you_go' ? 'Projected EOM' : 'Projected usage value'}
          value={formatCost(cost.data?.projected_eom ?? null)}
          hint={cost.data ? `${cost.data.days_remaining}d left` : undefined}
          loading={cost.isPending}
        />
      ) : (
        projectionPlaceholder(
          billingType === 'pay_as_you_go' ? 'Projected EOM' : 'Projected usage value',
        )
      )}
      {isLiveMonth ? (
        <StatTile
          label={billingType === 'pay_as_you_go' ? 'Daily burn (7d)' : 'Daily usage value (7d)'}
          value={formatCost(cost.data?.daily_burn_avg_7d ?? null)}
          loading={cost.isPending}
        />
      ) : (
        projectionPlaceholder(
          billingType === 'pay_as_you_go' ? 'Daily burn (7d)' : 'Daily usage value (7d)',
        )
      )}
      <StatTile
        label={`Tokens · ${scopeLabel}`}
        value={formatTokens(scopedTokens)}
        hint={bucket?.msgs != null ? `${bucket.msgs} msgs` : undefined}
        loading={bucketLoading}
      />
      <StatTile
        label={`Cache hit · ${scopeLabel}`}
        value={cacheHitPct != null ? formatPct(cacheHitPct) : '—'}
        loading={bucketLoading}
      />
      <StatTile
        label="Lifetime spend"
        value={formatCost(lifetimeCost)}
        loading={lifetimeLoading}
      />
    </div>
  );
}
