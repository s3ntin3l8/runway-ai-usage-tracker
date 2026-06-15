// Overview: "how am I doing right now?" — a KPI strip, any anomaly/error
// alerts, the quota-window gauges, and a compact fill trajectory for the
// critical window so the answer to "am I on pace?" is visible without a tab
// switch.

import { useMemo } from 'react';
import type { CumulativeBucket, FleetEntry, ForecastEntry, LimitCard } from '@/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { ModelDonut } from '@/components/charts/ModelDonut';
import { TokenDonut } from '@/components/charts/TokenDonut';
import { TrajectoryChart } from '@/components/charts/TrajectoryChart';
import { formatPct } from '@/lib/format';
import { ProviderAlerts } from './ProviderAlerts';
import { ProviderKpis } from './ProviderKpis';
import { ProviderTrendCard } from './ProviderTrendCard';
import { QuotaWindowRow } from './QuotaWindowRow';
import { RecentSessions } from './RecentSessions';
import { useProviderCumulative, useProviderForecast } from './queries';

// Match a quota card to its forecast: prefer an exact window/variant/model match,
// fall back to the window_type alone (so secondary limits and per-model variants
// each resolve to the right entry).
function findForecast(card: LimitCard, forecasts: ForecastEntry[]): ForecastEntry | null {
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

export function OverviewTab({ entry }: { entry: FleetEntry }) {
  // Cache read/create is ~95% of tokens and skews the headline stats; let the
  // user drop it from the month totals and both token donuts. Shared, persisted
  // pref so the choice carries across tabs and the Home strip.
  const { excludeCache } = useExcludeCache();
  const forecast = useProviderForecast(entry.provider_id, entry.account_id);
  const cumulative = useProviderCumulative(entry.provider_id, entry.account_id);
  const cards = [entry.critical_gauge, ...entry.secondary_limits];

  // Trajectory for the window we treat as critical (fall back to the first).
  const criticalForecast = useMemo(() => {
    const fs = forecast.data?.forecasts ?? [];
    return fs.find((f) => f.window_type === entry.critical_gauge.window_type) ?? fs[0] ?? null;
  }, [forecast.data, entry.critical_gauge.window_type]);

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
  const agg = entry.window_aggregations?.longest;
  const bySidecar = agg?.by_sidecar ?? {};
  const sourceIsSidecar = Object.keys(bySidecar).length > 1;
  const sourceSplit = sourceIsSidecar ? bySidecar : (agg?.by_model ?? {});
  const sourceTitle = sourceIsSidecar ? 'Active window by source' : 'Active window by model';
  const hasSourceSplit = Object.values(sourceSplit).length > 0;

  return (
    <div className="flex flex-col gap-4">
      <ExcludeCacheToggle />
      <ProviderKpis entry={entry} excludeCache={excludeCache} />
      <ProviderAlerts providerId={entry.provider_id} accountId={entry.account_id} />

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

      <ProviderTrendCard
        providerId={entry.provider_id}
        accountId={entry.account_id}
        metric="tokens"
        title="Tokens per day"
        defaultDays={14}
        compact
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
            {agg ? (
              <span className="text-[11px] text-fg-subtle">{agg.window_type} window</span>
            ) : null}
          </CardHeader>
          <CardContent>
            {hasSourceSplit ? (
              <ModelDonut byModel={sourceSplit} className="h-44" excludeCache={excludeCache} />
            ) : (
              <p className="py-12 text-center text-xs text-fg-subtle">
                No activity in the current window.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <RecentSessions providerId={entry.provider_id} accountId={entry.account_id} />
    </div>
  );
}
