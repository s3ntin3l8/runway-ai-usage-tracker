// Aggregate strip: month-to-date spend, EOM projection, 7-day burn, and
// this-month token/message volume — the "what is all this costing me" row.

import type { CostForecastResponse, CumulativeBucket, CumulativeResponse } from '@/api/types';
import { Card } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { formatCost, formatTokens } from '@/lib/format';

interface AggregateStripProps {
  cost: CostForecastResponse | undefined;
  cumulative: CumulativeResponse | undefined;
  loading: boolean;
}

function monthTotals(cumulative: CumulativeResponse | undefined): {
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
      (b.tokens_cache_read ?? 0) +
      (b.tokens_cache_create ?? 0) +
      (b.tokens_reasoning ?? 0);
    msgs += b.msgs ?? 0;
  }
  return { tokens, msgs };
}

export function AggregateStrip({ cost, cumulative, loading }: AggregateStripProps) {
  const { tokens, msgs } = monthTotals(cumulative);

  const stats: { label: string; value: string; hint?: string }[] = [
    {
      label: 'Spend (MTD)',
      value: formatCost(cost?.current_month_to_date ?? null),
    },
    {
      label: 'Projected EOM',
      value: formatCost(cost?.projected_eom ?? null),
      hint: cost ? `${cost.days_remaining}d left` : undefined,
    },
    {
      label: 'Daily burn (7d)',
      value: formatCost(cost?.daily_burn_avg_7d ?? null),
    },
    {
      label: 'Tokens this month',
      value: formatTokens(tokens),
      hint: msgs > 0 ? `${msgs.toLocaleString()} msgs` : undefined,
    },
  ];

  return (
    <section aria-label="Monthly aggregates" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {stats.map((stat) => (
        <Card key={stat.label} className="px-4 py-3">
          <p className="text-[11px] font-medium text-fg-subtle">{stat.label}</p>
          {loading ? (
            <Skeleton className="mt-1.5 h-6 w-20" />
          ) : (
            <div className="mt-0.5 flex items-baseline gap-2">
              <span className="font-mono text-lg font-semibold tabular">{stat.value}</span>
              {stat.hint ? <span className="text-[11px] text-fg-subtle">{stat.hint}</span> : null}
            </div>
          )}
        </Card>
      ))}
    </section>
  );
}
