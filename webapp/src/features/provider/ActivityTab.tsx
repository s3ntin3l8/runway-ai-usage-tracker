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
import { getUserTz } from '@/lib/tz';
import { TopProjectsCard } from '@/features/insights/TopProjectsCard';
import type { TabScope } from './period';
import { ProviderTrendCard } from './ProviderTrendCard';
import { SessionsTable } from './SessionsTable';
import {
  useProviderCumulative,
  useProviderCumulativeMonth,
  useProviderCumulativeRange,
  useProviderHeatmap,
  useProviderSessions,
} from './queries';

export function ActivityTab({
  providerId,
  accountId,
  scope,
}: {
  providerId: string;
  accountId: string;
  scope: TabScope;
}) {
  const { excludeCache } = useExcludeCache();
  // Every panel honours the one selected scope (month-to-date, a past month, or
  // a rolling window) — see issue #87.
  const range = scope.range;
  const heatmap = useProviderHeatmap(providerId, accountId, getUserTz(), range);
  const sessions = useProviderSessions(providerId, accountId, range);
  // The live, month-scoped and range-scoped cumulative responses all set
  // `current_month_key` to the bucket that holds the data, so the read below is
  // source-agnostic. Pick the source matching the active scope; the other hooks
  // stay disabled.
  const isRolling = scope.mode === 'rolling';
  const liveCumulative = useProviderCumulative(providerId, accountId);
  const monthCumulative = useProviderCumulativeMonth(
    providerId,
    accountId,
    scope.key,
    scope.mode === 'month' && !scope.isCurrentMonth,
  );
  const rangeCumulative = useProviderCumulativeRange(providerId, accountId, range, isRolling);
  const cumulative = isRolling
    ? rangeCumulative
    : scope.isCurrentMonth
      ? liveCumulative
      : monthCumulative;
  const scopeLabel = scope.label;

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
        title={`Tokens per day · ${scopeLabel}`}
        range={range}
        excludeCache={excludeCache}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Token composition · {scopeLabel}</CardTitle>
          </CardHeader>
          <CardContent>
            {cumulative.isPending ? (
              <Skeleton className="h-56 w-full" />
            ) : monthBucket ? (
              <TokenDonut bucket={monthBucket} excludeCache={excludeCache} />
            ) : (
              <p className="py-8 text-center text-xs text-fg-subtle">
                No usage recorded in {scopeLabel}.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Tokens by model · {scopeLabel}</CardTitle>
          </CardHeader>
          <CardContent>
            {cumulative.isPending ? (
              <Skeleton className="h-56 w-full" />
            ) : monthBucket?.by_model && Object.keys(monthBucket.by_model).length > 0 ? (
              <ModelDonut byModel={monthBucket.by_model} excludeCache={excludeCache} />
            ) : (
              <p className="py-8 text-center text-xs text-fg-subtle">
                No per-model usage in {scopeLabel}.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{`Activity by hour · ${scopeLabel}`}</CardTitle>
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
              {`No event activity in ${scopeLabel}.`}
            </p>
          )}
        </CardContent>
      </Card>

      <TopProjectsCard
        range={scope.range}
        providerId={providerId}
        title={`Top projects · ${scopeLabel}`}
      />

      <Card>
        <CardHeader>
          <CardTitle>{`Top sessions · ${scopeLabel}`}</CardTitle>
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
