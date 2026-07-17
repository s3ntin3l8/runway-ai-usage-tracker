// Aggregate strip: month-to-date spend, EOM projection, 7-day burn, and
// this-month token/message volume — the "what is all this costing me" row.

import type { CostForecastResponse, CumulativeBucket, CumulativeResponse } from '@/api/types';
import { Card } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatCost, formatTokens } from '@/lib/format';

interface AggregateStripProps {
  cost: CostForecastResponse | undefined;
  cumulative: CumulativeResponse | undefined;
  // Split so the three cost-forecast cards don't wait on the (slower)
  // cumulative token/msg query — they resolve independently.
  costLoading: boolean;
  tokensLoading: boolean;
}

function monthTotals(
  cumulative: CumulativeResponse | undefined,
  excludeCache: boolean,
): {
  tokens: number;
  msgs: number;
} {
  if (!cumulative) return { tokens: 0, msgs: 0 };
  let tokens = 0;
  let msgs = 0;
  for (const entry of cumulative.cumulative) {
    const bucket = entry[cumulative.current_month_key];
    if (!bucket || typeof bucket === 'string') continue;
    const b = bucket as CumulativeBucket;
    tokens +=
      (b.tokens_input ?? 0) +
      (b.tokens_output ?? 0) +
      (excludeCache ? 0 : (b.tokens_cache_read ?? 0) + (b.tokens_cache_create ?? 0)) +
      (b.tokens_reasoning ?? 0);
    msgs += b.msgs ?? 0;
  }
  return { tokens, msgs };
}

export function AggregateStrip({
  cost,
  cumulative,
  costLoading,
  tokensLoading,
}: AggregateStripProps) {
  const { excludeCache } = useExcludeCache();
  const { tokens, msgs } = monthTotals(cumulative, excludeCache);

  const stats: { label: string; value: string; hint?: string; loading: boolean }[] = [
    {
      label: 'Spend (MTD)',
      value: formatCost(cost?.current_month_to_date ?? null),
      loading: costLoading,
    },
    {
      label: 'Projected EOM',
      value: formatCost(cost?.projected_eom ?? null),
      hint: cost ? `${cost.days_remaining}d left` : undefined,
      loading: costLoading,
    },
    {
      label: 'Daily burn (7d)',
      value: formatCost(cost?.daily_burn_avg_7d ?? null),
      loading: costLoading,
    },
    {
      label: 'Tokens this month',
      value: formatTokens(tokens),
      hint: msgs > 0 ? `${msgs.toLocaleString()} msgs` : undefined,
      loading: tokensLoading,
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <ExcludeCacheToggle />
      <section aria-label="Monthly aggregates" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {stats.map((stat) => (
          <Card key={stat.label} className="px-4 py-3">
            <p className="text-[11px] font-medium text-fg-subtle">{stat.label}</p>
            {stat.loading ? (
              <Skeleton className="mt-1.5 h-6 w-20" />
            ) : (
              <div className="mt-0.5 flex items-baseline gap-2">
                <span className="font-mono text-lg font-semibold tabular">{stat.value}</span>
                {stat.hint ? (
                  <span className="text-[11px] text-fg-subtle">{stat.hint}</span>
                ) : null}
              </div>
            )}
          </Card>
        ))}
      </section>
    </div>
  );
}
