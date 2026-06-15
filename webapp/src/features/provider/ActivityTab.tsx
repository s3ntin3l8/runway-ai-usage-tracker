// Activity: "what have I been doing?" — per-day token volume, the token
// composition this month, the hour×weekday heatmap and top sessions (with
// subagent splits). The raw per-message event tail lives in the Events tab.

import { useMemo } from 'react';
import type { CumulativeBucket } from '@/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { ModelDonut } from '@/components/charts/ModelDonut';
import { TokenDonut } from '@/components/charts/TokenDonut';
import { UsageHeatmap } from '@/components/charts/UsageHeatmap';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatLocalDate, getUserTz } from '@/lib/tz';
import { TopProjectsCard } from '@/features/history/TopProjectsCard';
import type { SelectedPeriod } from './period';
import { ProviderTrendCard } from './ProviderTrendCard';
import { SessionsTable } from './SessionsTable';
import {
  useProviderCumulative,
  useProviderCumulativeMonth,
  useProviderHeatmap,
  useProviderSessions,
} from './queries';

export function ActivityTab({
  providerId,
  accountId,
  period,
}: {
  providerId: string;
  accountId: string;
  period: SelectedPeriod;
}) {
  const { excludeCache } = useExcludeCache();
  // Current month keeps the live (rolling) calls; a past month is scoped to its
  // closed [since, until) range so every panel reflects that month.
  const range = period.isCurrentMonth ? undefined : period.range;
  const heatmap = useProviderHeatmap(providerId, accountId, getUserTz(), range);
  const sessions = useProviderSessions(providerId, accountId, range);
  // Both the live and month-scoped cumulative responses set `current_month_key`
  // to the bucket that holds the data, so the read below is branch-agnostic.
  const liveCumulative = useProviderCumulative(providerId, accountId);
  const monthCumulative = useProviderCumulativeMonth(
    providerId,
    accountId,
    period.key,
    !period.isCurrentMonth,
  );
  const cumulative = period.isCurrentMonth ? liveCumulative : monthCumulative;
  const monthLabel = formatLocalDate(period.range.since, { month: 'long', year: 'numeric' });

  const monthBucket = useMemo<CumulativeBucket | null>(() => {
    const data = cumulative.data;
    if (!data) return null;
    const row = data.cumulative.find(
      (c) => c.provider_id === providerId && c.account_id === accountId,
    );
    const bucket = row?.[data.current_month_key];
    return bucket && typeof bucket !== 'string' ? bucket : null;
  }, [cumulative.data, providerId, accountId]);

  return (
    <div className="flex flex-col gap-4">
      <ExcludeCacheToggle />
      <ProviderTrendCard
        providerId={providerId}
        accountId={accountId}
        metric="tokens"
        title={`Tokens per day · ${monthLabel}`}
        range={range}
        excludeCache={excludeCache}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Token composition · {monthLabel}</CardTitle>
          </CardHeader>
          <CardContent>
            {cumulative.isPending ? (
              <Skeleton className="h-56 w-full" />
            ) : monthBucket ? (
              <TokenDonut bucket={monthBucket} excludeCache={excludeCache} />
            ) : (
              <p className="py-8 text-center text-xs text-fg-subtle">
                No usage recorded in {monthLabel}.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Tokens by model · {monthLabel}</CardTitle>
          </CardHeader>
          <CardContent>
            {cumulative.isPending ? (
              <Skeleton className="h-56 w-full" />
            ) : monthBucket?.by_model && Object.keys(monthBucket.by_model).length > 0 ? (
              <ModelDonut byModel={monthBucket.by_model} excludeCache={excludeCache} />
            ) : (
              <p className="py-8 text-center text-xs text-fg-subtle">
                No per-model usage in {monthLabel}.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{range ? `Activity by hour · ${monthLabel}` : 'Activity by hour (14 days)'}</CardTitle>
          {heatmap.data ? (
            <span className="text-[11px] text-fg-subtle">{heatmap.data.tz}</span>
          ) : null}
        </CardHeader>
        <CardContent>
          {heatmap.isPending ? (
            <Skeleton className="h-56 w-full" />
          ) : heatmap.data && heatmap.data.cells.some((c) => c.tokens > 0) ? (
            <UsageHeatmap cells={heatmap.data.cells} />
          ) : (
            <p className="py-8 text-center text-xs text-fg-subtle">
              {range ? `No event activity in ${monthLabel}.` : 'No event activity in the last 14 days.'}
            </p>
          )}
        </CardContent>
      </Card>

      <TopProjectsCard
        range={period.range}
        providerId={providerId}
        title={`Top projects · ${monthLabel}`}
      />

      <Card>
        <CardHeader>
          <CardTitle>{range ? `Top sessions · ${monthLabel}` : 'Top sessions (7 days)'}</CardTitle>
        </CardHeader>
        {sessions.isPending ? (
          <CardContent>
            <Skeleton className="h-24 w-full" />
          </CardContent>
        ) : (sessions.data?.sessions.length ?? 0) === 0 ? (
          <CardContent>
            <p className="py-4 text-center text-xs text-fg-subtle">
              No sessions recorded — session data needs a sidecar feeding events.
            </p>
          </CardContent>
        ) : (
          <SessionsTable sessions={sessions.data!.sessions} excludeCache={excludeCache} />
        )}
      </Card>
    </div>
  );
}
