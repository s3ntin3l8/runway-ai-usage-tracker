// Cost: MTD spend + EOM projection for this provider, per-model and
// per-sidecar splits from the cumulative month bucket. The EOM/burn projections
// are forward-looking, so they only apply to the current month — a selected
// past month falls back to that month's recorded spend.

import { useMemo, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import type { CumulativeBucket, CumulativeModelBucket } from '@/api/types';
import { CostDonut, modelCost } from '@/components/charts/CostDonut';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatTile } from '@/components/ui/StatTile';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { cn } from '@/lib/cn';
import { formatCost, formatTokens } from '@/lib/format';
import { DetailSection, Stat } from './detailPrimitives';
import type { TabScope } from './period';
import { ProviderTrendCard } from './ProviderTrendCard';
import {
  useProviderCostForecast,
  useProviderCumulative,
  useProviderCumulativeMonth,
  useProviderCumulativeRange,
} from './queries';

export function CostTab({
  providerId,
  accountId,
  scope,
}: {
  providerId: string;
  accountId: string;
  scope: TabScope;
}) {
  const { excludeCache } = useExcludeCache();
  const range = scope.range;
  // EOM / daily-burn projections are forward-looking, so they only apply to the
  // live calendar month — a past month or a rolling window shows recorded spend.
  const isLiveMonth = scope.mode === 'month' && scope.isCurrentMonth;
  const isRolling = scope.mode === 'rolling';
  const cost = useProviderCostForecast(providerId, accountId);
  // `liveCumulative` is always fetched for the scope-independent Lifetime tile
  // (the month/range-scoped responses carry no lifetime bucket).
  const liveCumulative = useProviderCumulative(providerId, accountId);
  const monthCumulative = useProviderCumulativeMonth(
    providerId,
    accountId,
    scope.key,
    scope.mode === 'month' && !scope.isCurrentMonth,
  );
  const rangeCumulative = useProviderCumulativeRange(providerId, accountId, range, isRolling);
  const cumulative = isRolling ? rangeCumulative : isLiveMonth ? liveCumulative : monthCumulative;
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

  const lifetime = useMemo<CumulativeBucket | null>(() => {
    const row = liveCumulative.data?.cumulative.find(
      (c) => c.provider_id === providerId && c.account_id === accountId,
    );
    return row?.lifetime ?? null;
  }, [liveCumulative.data, providerId, accountId]);

  const stats = isLiveMonth
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
        { label: `Spend · ${scopeLabel}`, value: formatCost(monthBucket?.cost_usd ?? null) },
        { label: 'Projected EOM', value: '—', hint: 'current month only' },
        { label: 'Daily burn (7d)', value: '—', hint: 'current month only' },
        { label: 'Lifetime', value: formatCost(lifetime?.cost_usd ?? null) },
      ];
  const statsLoading = isLiveMonth
    ? cost.isPending || cumulative.isPending
    : cumulative.isPending || liveCumulative.isPending;

  return (
    <div className="flex flex-col gap-4">
      <ExcludeCacheToggle />
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
        title={`Cost per day · ${scopeLabel}`}
        range={range}
        excludeCache={excludeCache}
      />

      <SplitTable
        title={`Cost by model · ${scopeLabel}`}
        split={monthBucket?.by_model}
        loading={cumulative.isPending}
        nameHeader="Model"
        scopeLabel={scopeLabel}
      />
      <SplitTable
        title={`Cost by sidecar · ${scopeLabel}`}
        split={monthBucket?.by_sidecar}
        loading={cumulative.isPending}
        nameHeader="Sidecar"
        scopeLabel={scopeLabel}
      />
    </div>
  );
}

function SplitTable({
  title,
  split,
  loading,
  nameHeader,
  scopeLabel,
}: {
  title: string;
  split: CumulativeBucket['by_model'] | undefined | null;
  loading: boolean;
  nameHeader: string;
  scopeLabel: string;
}) {
  const { excludeCache } = useExcludeCache();
  const rows = Object.entries(split ?? {}).sort(
    ([, a], [, b]) => modelCost(b, excludeCache) - modelCost(a, excludeCache),
  );
  // Reasoning is rarely populated — only show its column when some row has it.
  const hasReasoning = rows.some(([, b]) => (b.tokens_reasoning ?? 0) > 0);
  // colSpan for the expandable detail row: chevron + name + messages + input +
  // output + cost (6), plus cache read/write (2) and reasoning (1) when shown.
  const colSpan = 6 + (excludeCache ? 0 : 2) + (hasReasoning ? 1 : 0);
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
          <p className="py-4 text-center text-xs text-fg-subtle">No cost data in {scopeLabel}.</p>
        </CardContent>
      ) : (
        <>
          <CardContent>
            <CostDonut data={split ?? {}} className="h-44" excludeCache={excludeCache} />
          </CardContent>
          <div className="overflow-x-auto">
            <Table>
              <THead>
                <TR>
                  <TH className="w-8" />
                  <TH>{nameHeader}</TH>
                  <TH className="text-right">Messages</TH>
                  <TH className="text-right">Input</TH>
                  <TH className="text-right">Output</TH>
                  {excludeCache ? null : (
                    <>
                      <TH className="text-right">Cache read</TH>
                      <TH className="text-right">Cache write</TH>
                    </>
                  )}
                  {hasReasoning ? <TH className="text-right">Reasoning</TH> : null}
                  <TH className="text-right">Cost</TH>
                </TR>
              </THead>
              <TBody>
                {rows.map(([name, b]) => (
                  <SplitRow
                    key={name}
                    name={name}
                    b={b}
                    excludeCache={excludeCache}
                    hasReasoning={hasReasoning}
                    colSpan={colSpan}
                  />
                ))}
              </TBody>
            </Table>
          </div>
        </>
      )}
    </Card>
  );
}

// One model/sidecar row. Collapsed it mirrors the token columns; expanding it
// reveals the cost split per token category (reasoning folds into Output, billed
// at the output rate). Cache cost cells drop out under the exclude-cache toggle,
// matching the collapsed token columns and the Cost total.
function SplitRow({
  name,
  b,
  excludeCache,
  hasReasoning,
  colSpan,
}: {
  name: string;
  b: CumulativeModelBucket;
  excludeCache: boolean;
  hasReasoning: boolean;
  colSpan: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <TR
        className="cursor-pointer hover:bg-surface-2/50"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <TD className="w-8 pr-0 text-fg-subtle">
          <ChevronRight
            className={cn('size-4 transition-transform duration-150', open && 'rotate-90')}
          />
        </TD>
        <TD className="font-medium">{name}</TD>
        <TD className="text-right font-mono tabular">{b.msgs ?? 0}</TD>
        <TD className="text-right font-mono tabular">{formatTokens(b.tokens_input ?? 0)}</TD>
        <TD className="text-right font-mono tabular">{formatTokens(b.tokens_output ?? 0)}</TD>
        {excludeCache ? null : (
          <>
            <TD className="text-right font-mono tabular">
              {formatTokens(b.tokens_cache_read ?? 0)}
            </TD>
            <TD className="text-right font-mono tabular">
              {formatTokens(b.tokens_cache_create ?? 0)}
            </TD>
          </>
        )}
        {hasReasoning ? (
          <TD className="text-right font-mono tabular">
            {formatTokens(b.tokens_reasoning ?? 0)}
          </TD>
        ) : null}
        <TD className="text-right font-mono tabular">{formatCost(modelCost(b, excludeCache))}</TD>
      </TR>
      {open ? (
        <TR className="hover:bg-transparent">
          <TD colSpan={colSpan} className="p-0">
            <div className="bg-surface-2/40 px-4 py-4">
              <DetailSection title="Cost breakdown">
                <div className="grid grid-cols-3 gap-3 sm:grid-cols-5">
                  <Stat label="Input $" value={formatCost(b.cost_input ?? 0)} />
                  <Stat label="Output $" value={formatCost(b.cost_output ?? 0)} />
                  {excludeCache ? null : (
                    <>
                      <Stat label="Cache read $" value={formatCost(b.cost_cache_read ?? 0)} />
                      <Stat label="Cache write $" value={formatCost(b.cost_cache_create ?? 0)} />
                    </>
                  )}
                  <Stat label="Total $" value={formatCost(modelCost(b, excludeCache))} />
                </div>
              </DetailSection>
            </div>
          </TD>
        </TR>
      ) : null}
    </>
  );
}
