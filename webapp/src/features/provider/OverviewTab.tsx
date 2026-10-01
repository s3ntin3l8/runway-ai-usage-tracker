// Overview: "how am I doing right now?" — a KPI strip, any anomaly/error
// alerts, the quota-window gauges, and a compact fill trajectory for the
// critical window so the answer to "am I on pace?" is visible without a tab
// switch.

import { useMemo } from 'react';
import type { CumulativeBucket, FleetEntry } from '@/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { ModelDonut } from '@/components/charts/ModelDonut';
import { TokenBar } from '@/components/charts/TokenBar';
import { TokenDonut } from '@/components/charts/TokenDonut';
import { TrajectoryChart } from '@/components/charts/TrajectoryChart';
import { hasTokenData, sumTokens } from '@/lib/cumulative';
import { formatNumber, formatPct, formatTokens } from '@/lib/format';
import { cardKind, findForecast } from '@/lib/quota';
import { CostOutlookCard } from './CostOutlookCard';
import { ProviderAlerts } from './ProviderAlerts';
import { ProviderKpis } from './ProviderKpis';
import { ProviderTrendCard } from './ProviderTrendCard';
import { QuotaWindowRow } from './QuotaWindowRow';
import { RecentSessions } from './RecentSessions';
import type { TabScope } from './period';
import {
  useProviderCumulative,
  useProviderCumulativeMonth,
  useProviderCumulativeRange,
  useProviderForecast,
} from './queries';


export function OverviewTab({ entry, scope }: { entry: FleetEntry; scope: TabScope }) {
  // Cache read/create is ~95% of tokens and skews the headline stats; let the
  // user drop it from the month totals and both token donuts. Shared, persisted
  // pref so the choice carries across tabs and the Home strip.
  const { excludeCache } = useExcludeCache();
  const forecast = useProviderForecast(entry.provider_id, entry.account_id);
  // Scope-matching bucket source — same 3-way split as ProviderKpis (which
  // shares the React Query cache), so the donuts and KPI tiles agree.
  const isLiveMonth = scope.isLiveMonth;
  const isRange = !scope.isLiveMonth && !scope.periodKey;
  const liveCumulative = useProviderCumulative(entry.provider_id, entry.account_id);
  const monthCumulative = useProviderCumulativeMonth(
    entry.provider_id,
    entry.account_id,
    scope.periodKey ?? '',
    !!scope.periodKey && !scope.isLiveMonth,
  );
  const rangeCumulative = useProviderCumulativeRange(
    entry.provider_id,
    entry.account_id,
    scope.range,
    isRange,
  );
  const cumulative = isLiveMonth
    ? liveCumulative
    : scope.periodKey
      ? monthCumulative
      : rangeCumulative;
  const scopeLabel = scope.label;
  const cards = [entry.critical_gauge, ...entry.secondary_limits];
  const kind = cardKind(entry.critical_gauge);

  // Trajectory for the window we treat as critical. Match on the full card
  // identity (window_type + variant + model_id) via findForecast, not window_type
  // alone — providers like Antigravity emit two pools (gemini/frontier) per
  // window, and a window_type-only match can land on the empty pool's
  // insufficient-data forecast instead of the gauge's own.
  const criticalForecast = useMemo(() => {
    const fs = forecast.data?.forecasts ?? [];
    return findForecast(entry.critical_gauge, fs);
  }, [forecast.data, entry.critical_gauge]);

  // This scope's bucket for the token-mix donut — same lookup as ProviderKpis,
  // so React Query serves it from cache (no extra request).
  const scopeBucket = useMemo<CumulativeBucket | null>(() => {
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
  const sourceSplit = useWindowSplit ? windowSplit : (scopeBucket?.by_model ?? {});
  const sourceTitle = useWindowSplit
    ? (sourceIsSidecar ? 'Active window by source' : 'Active window by model')
    : `Tokens by model · ${scopeLabel}`;
  const hasSourceSplit = Object.keys(sourceSplit).length > 0;

  return (
    <div className="flex flex-col gap-4">
      <ProviderKpis entry={entry} scope={scope} excludeCache={excludeCache} />
      <ProviderAlerts providerId={entry.provider_id} accountId={entry.account_id} />
      {entry.server_collector_available && entry.critical_gauge.data_source === 'local' ? (
        <Card role="status" className="border-warning/30 bg-warning-muted px-4 py-2.5 text-[13px] text-fg">
          Usage events are available, but quota data has not been collected. Check the provider credentials in Settings or use Debug to inspect collection.
        </Card>
      ) : null}

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
            <CardTitle>Token usage · {scopeLabel}</CardTitle>
            <span className="text-[11px] text-fg-subtle">Recorded usage</span>
          </CardHeader>
          <CardContent>
            <div className="flex items-baseline gap-3">
              <span className="font-mono text-2xl font-semibold tabular">
                {formatTokens(sumTokens(scopeBucket, excludeCache))}
              </span>
              <span className="text-xs text-fg-subtle">tokens</span>
            </div>
            <TokenBar
              tokens={{
                tokens_input: scopeBucket?.tokens_input,
                tokens_output: scopeBucket?.tokens_output,
                tokens_cache_read: scopeBucket?.tokens_cache_read,
                tokens_cache_create: scopeBucket?.tokens_cache_create,
                tokens_reasoning: scopeBucket?.tokens_reasoning,
              }}
              showLegend
              excludeCache={excludeCache}
              className="mt-3"
            />
            {scopeBucket?.msgs != null ? (
              <p className="mt-2 text-[11px] text-fg-subtle">
                {formatNumber(scopeBucket.msgs)} messages · {scopeLabel}
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
        title={`Tokens per day · ${scopeLabel}`}
        range={scope.range}
        compact
        excludeCache={excludeCache}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Token mix · {scopeLabel}</CardTitle>
          </CardHeader>
          <CardContent>
            {cumulative.isPending ? (
              <Skeleton className="h-44 w-full" />
            ) : hasTokenData(scopeBucket, excludeCache) ? (
              <TokenDonut bucket={scopeBucket} className="h-44" excludeCache={excludeCache} />
            ) : (
              <p className="py-12 text-center text-xs text-fg-subtle">
                No usage in {scopeLabel}.
              </p>
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
                {useWindowSplit ? 'No activity in the current window.' : `No usage in ${scopeLabel}.`}
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <RecentSessions
        providerId={entry.provider_id}
        accountId={entry.account_id}
        excludeCache={excludeCache}
        range={scope.range}
        label={scopeLabel}
      />
    </div>
  );
}
