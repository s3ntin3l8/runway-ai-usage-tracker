// Cost: MTD spend + EOM projection for this provider, per-model and
// per-sidecar splits from the cumulative month bucket. The EOM/burn projections
// are forward-looking, so they only apply to the current month — a selected
// past month falls back to that month's recorded spend.

import { useMemo } from 'react';
import type { CumulativeBucket } from '@/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatTile } from '@/components/ui/StatTile';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { formatCost, formatTokens } from '@/lib/format';
import { formatLocalDate } from '@/lib/tz';
import type { SelectedPeriod } from './period';
import { ProviderTrendCard } from './ProviderTrendCard';
import {
  useProviderCostForecast,
  useProviderCumulative,
  useProviderCumulativeMonth,
} from './queries';

export function CostTab({
  providerId,
  accountId,
  period,
}: {
  providerId: string;
  accountId: string;
  period: SelectedPeriod;
}) {
  const range = period.isCurrentMonth ? undefined : period.range;
  const cost = useProviderCostForecast(providerId, accountId);
  // `liveCumulative` is always fetched for the period-independent Lifetime tile
  // (the month-scoped response carries no lifetime bucket).
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

  const lifetime = useMemo<CumulativeBucket | null>(() => {
    const row = liveCumulative.data?.cumulative.find(
      (c) => c.provider_id === providerId && c.account_id === accountId,
    );
    return row?.lifetime ?? null;
  }, [liveCumulative.data, providerId, accountId]);

  const stats = period.isCurrentMonth
    ? [
        { label: 'Spend (MTD)', value: formatCost(cost.data?.current_month_to_date ?? null) },
        {
          label: 'Projected EOM',
          value: formatCost(cost.data?.projected_eom ?? null),
          hint: cost.data ? `${cost.data.days_remaining}d left` : undefined,
        },
        { label: 'Daily burn (7d)', value: formatCost(cost.data?.daily_burn_avg_7d ?? null) },
        { label: 'Lifetime', value: formatCost(lifetime?.cost_usd ?? null) },
      ]
    : [
        { label: `Spend · ${monthLabel}`, value: formatCost(monthBucket?.cost_usd ?? null) },
        { label: 'Projected EOM', value: '—', hint: 'current month only' },
        { label: 'Daily burn (7d)', value: '—', hint: 'current month only' },
        { label: 'Lifetime', value: formatCost(lifetime?.cost_usd ?? null) },
      ];
  const statsLoading = period.isCurrentMonth
    ? cost.isPending || cumulative.isPending
    : cumulative.isPending || liveCumulative.isPending;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {stats.map((stat) => (
          <StatTile
            key={stat.label}
            label={stat.label}
            value={stat.value}
            hint={stat.hint}
            loading={statsLoading}
          />
        ))}
      </div>

      <ProviderTrendCard
        providerId={providerId}
        accountId={accountId}
        metric="cost"
        title={`Cost per day · ${monthLabel}`}
        range={range}
      />

      <SplitTable
        title={`Cost by model · ${monthLabel}`}
        split={monthBucket?.by_model}
        loading={cumulative.isPending}
        nameHeader="Model"
        monthLabel={monthLabel}
      />
      <SplitTable
        title={`Cost by sidecar · ${monthLabel}`}
        split={monthBucket?.by_sidecar}
        loading={cumulative.isPending}
        nameHeader="Sidecar"
        monthLabel={monthLabel}
      />
    </div>
  );
}

function SplitTable({
  title,
  split,
  loading,
  nameHeader,
  monthLabel,
}: {
  title: string;
  split: CumulativeBucket['by_model'] | undefined | null;
  loading: boolean;
  nameHeader: string;
  monthLabel: string;
}) {
  const rows = Object.entries(split ?? {}).sort(
    ([, a], [, b]) => (b.cost_usd ?? 0) - (a.cost_usd ?? 0),
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      {loading ? (
        <CardContent>
          <Skeleton className="h-24 w-full" />
        </CardContent>
      ) : rows.length === 0 ? (
        <CardContent>
          <p className="py-4 text-center text-xs text-fg-subtle">No cost data in {monthLabel}.</p>
        </CardContent>
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>{nameHeader}</TH>
              <TH className="text-right">Messages</TH>
              <TH className="text-right">Tokens</TH>
              <TH className="text-right">Cost</TH>
            </TR>
          </THead>
          <TBody>
            {rows.map(([name, b]) => (
              <TR key={name}>
                <TD className="font-medium">{name}</TD>
                <TD className="text-right font-mono tabular">{b.msgs ?? 0}</TD>
                <TD className="text-right font-mono tabular">
                  {formatTokens(
                    (b.tokens_input ?? 0) +
                      (b.tokens_output ?? 0) +
                      (b.tokens_cache_read ?? 0) +
                      (b.tokens_cache_create ?? 0) +
                      (b.tokens_reasoning ?? 0),
                  )}
                </TD>
                <TD className="text-right font-mono tabular">{formatCost(b.cost_usd)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </Card>
  );
}
