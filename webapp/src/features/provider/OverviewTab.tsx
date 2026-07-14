// Overview: "how am I doing right now?" — a KPI strip, any anomaly/error
// alerts, the quota-window gauges, and a compact fill trajectory for the
// critical window so the answer to "am I on pace?" is visible without a tab
// switch.

import { useMemo } from 'react';
import type { CumulativeBucket, FleetEntry } from '@/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { ModelDonut } from '@/components/charts/ModelDonut';
import { TokenBar } from '@/components/charts/TokenBar';
import { TokenDonut } from '@/components/charts/TokenDonut';
import { TrajectoryChart } from '@/components/charts/TrajectoryChart';
import { formatNumber, formatPct, formatTokens } from '@/lib/format';
import { cardKind, findForecast, tokenUsageTotal, windowLabel } from '@/lib/quota';
import { CostOutlookCard } from './CostOutlookCard';
import { ProviderAlerts } from './ProviderAlerts';
import { ProviderKpis } from './ProviderKpis';
import { ProviderTrendCard } from './ProviderTrendCard';
import { QuotaWindowRow } from './QuotaWindowRow';
import { RecentSessions } from './RecentSessions';
import { useProviderCumulative, useProviderForecast } from './queries';


export function OverviewTab({ entry }: { entry: FleetEntry }) {
  // Cache read/create is ~95% of tokens and skews the headline stats; let the
  // user drop it from the month totals and both token donuts. Shared, persisted
  // pref so the choice carries across tabs and the Home strip.
  const { excludeCache } = useExcludeCache();
  const forecast = useProviderForecast(entry.provider_id, entry.account_id);
  const cumulative = useProviderCumulative(entry.provider_id, entry.account_id);
  const cards = [entry.critical_gauge, ...entry.secondary_limits];
  const kind = cardKind(entry.critical_gauge);
  const critical = entry.critical_gauge;

  // Trajectory for the window we treat as critical. Match on the full card
  // identity (window_type + variant + model_id) via findForecast, not window_type
  // alone — providers like Antigravity emit two pools (gemini/frontier) per
  // window, and a window_type-only match can land on the empty pool's
  // insufficient-data forecast instead of the gauge's own.
  const criticalForecast = useMemo(() => {
    const fs = forecast.data?.forecasts ?? [];
    return findForecast(entry.critical_gauge, fs);
  }, [forecast.data, entry.critical_gauge]);

  // This month's bucket for the token-mix donut — same lookup as ProviderKpis,
  // so React Query serves it from cache (no extra request).
  const monthBucket = useMemo<CumulativeBucket | null>(() => {
    const data = cumulative.data;
    if (!data) return null;
    const row = data.cumulative.find(
      (c) => c.provider_id === entry.provider_id && c.account_id === entry.account_id,
    );
    const bucket = row?.[data.current_month_key];
    return bucket && typeof bucket !== 'string' ? bucket : null;
  }, [cumulative.data, entry.provider_id, entry.account_id]);

  // Live split of the longest active quota window. Prefer the per-sidecar split
  // when more than one machine feeds this provider; otherwise fall back to the
  // per-model split so single-host setups still get a useful breakdown.
  // For token/spend providers there is no active quota window, so window_aggregations
  // is empty — fall back to the cumulative month bucket's by_model instead.
  const agg = entry.window_aggregations?.longest;
  const bySidecar = agg?.by_sidecar ?? {};
  const sourceIsSidecar = Object.keys(bySidecar).length > 1;
  const windowSplit = sourceIsSidecar ? bySidecar : (agg?.by_model ?? {});
  const useWindowSplit = kind === 'quota';
  const sourceSplit = useWindowSplit ? windowSplit : (monthBucket?.by_model ?? {});
  const sourceTitle = useWindowSplit
    ? (sourceIsSidecar ? 'Active window by source' : 'Active window by model')
    : 'Tokens by model (month)';
  const hasSourceSplit = Object.keys(sourceSplit).length > 0;

  return (
    <div className="flex flex-col gap-4">
      <ExcludeCacheToggle />
      <ProviderKpis entry={entry} excludeCache={excludeCache} />
      <ProviderAlerts providerId={entry.provider_id} accountId={entry.account_id} />

      {kind === 'quota' && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Quota windows</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {cards.map((card, i) => (
                <QuotaWindowRow
                  key={`${card.service_name}-${card.window_type}-${i}`}
                  card={card}
                  siblings={cards}
                  forecast={findForecast(card, forecast.data?.forecasts ?? [])}
                />
              ))}
            </CardContent>
          </Card>

          <Card className="flex flex-col">
            <CardHeader>
              <CardTitle>Current window</CardTitle>
              {criticalForecast ? (
                <span className="text-[11px] text-fg-subtle">
                  projected {formatPct(criticalForecast.projected_pct)} at reset
                </span>
              ) : null}
            </CardHeader>
            <CardContent className="min-h-[11rem] flex-1">
              {forecast.isPending ? (
                <Skeleton className="h-full min-h-[11rem] w-full" />
              ) : criticalForecast ? (
                <TrajectoryChart forecast={criticalForecast} className="h-full min-h-[11rem] w-full" />
              ) : (
                <div className="flex h-full min-h-[11rem] items-center justify-center">
                  <p className="text-center text-xs text-fg-subtle">No trajectory yet.</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {kind === 'tokens' && (
        <Card>
          <CardHeader>
            <CardTitle>Token usage</CardTitle>
            <span className="text-[11px] text-fg-subtle">
              {windowLabel(critical) ?? 'all time'}
            </span>
          </CardHeader>
          <CardContent>
            <div className="flex items-baseline gap-3">
              <span className="font-mono text-2xl font-semibold tabular">
                {formatTokens(
                  tokenUsageTotal(critical.token_usage, excludeCache) ?? critical.used_value ?? null,
                )}
              </span>
              <span className="text-xs text-fg-subtle">tokens</span>
            </div>
            <TokenBar
              tokens={{
                tokens_input: critical.token_usage?.input,
                tokens_output: critical.token_usage?.output,
                tokens_cache_read: critical.token_usage?.cache_read,
                tokens_reasoning: critical.token_usage?.reasoning,
              }}
              showLegend
              className="mt-3"
            />
            {critical.msgs != null ? (
              <p className="mt-2 text-[11px] text-fg-subtle">
                {formatNumber(critical.msgs)} messages
              </p>
            ) : null}
          </CardContent>
        </Card>
      )}

      {kind === 'spend' && (
        <CostOutlookCard
          providerId={entry.provider_id}
          accountId={entry.account_id}
        />
      )}

      <ProviderTrendCard
        providerId={entry.provider_id}
        accountId={entry.account_id}
        metric="tokens"
        title="Tokens per day"
        defaultDays={14}
        compact
        excludeCache={excludeCache}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Token mix (month)</CardTitle>
          </CardHeader>
          <CardContent>
            {cumulative.isPending ? (
              <Skeleton className="h-44 w-full" />
            ) : monthBucket ? (
              <TokenDonut bucket={monthBucket} className="h-44" excludeCache={excludeCache} />
            ) : (
              <p className="py-12 text-center text-xs text-fg-subtle">No usage this month.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{sourceTitle}</CardTitle>
            {useWindowSplit && agg ? (
              <span className="text-[11px] text-fg-subtle">{agg.window_type} window</span>
            ) : null}
          </CardHeader>
          <CardContent>
            {!useWindowSplit && cumulative.isPending ? (
              <Skeleton className="h-44 w-full" />
            ) : hasSourceSplit ? (
              <ModelDonut byModel={sourceSplit} className="h-44" excludeCache={excludeCache} />
            ) : (
              <p className="py-12 text-center text-xs text-fg-subtle">
                {useWindowSplit ? 'No activity in the current window.' : 'No usage this month.'}
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <RecentSessions
        providerId={entry.provider_id}
        accountId={entry.account_id}
        excludeCache={excludeCache}
      />
    </div>
  );
}
