// Global, cross-provider lifetime stats — the "all of it, everywhere" strip
// that the per-provider tiles and month-scoped AggregateStrip don't cover.
// Token/cost tiles respect the shared exclude-cache toggle; session averages
// stay full (no per-session cache split is available client-side).

import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { StatTile } from '@/components/ui/StatTile';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatCost, formatCount, formatPct, formatTokens } from '@/lib/format';
import type { GlobalStatsResponse } from '@/api/types';
import { CircleAlert } from 'lucide-react';

function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, '0')}:00`;
}

export function GlobalInsights({
  stats,
  loading,
  error,
  onRetry,
}: {
  stats: GlobalStatsResponse | undefined;
  loading: boolean;
  // A failed request must not read as "these lifetime numbers are zero".
  error?: Error | null;
  onRetry?: () => void;
}) {
  const { excludeCache } = useExcludeCache();
  const life = stats?.lifetime;

  const lifetimeTokens = life
    ? life.tokens_total - (excludeCache ? life.tokens_cache : 0)
    : 0;
  const lifetimeCost = life ? life.cost_usd - (excludeCache ? life.cost_cache : 0) : 0;

  const tiles: { label: string; value: string; hint?: string }[] = [
    {
      label: 'Lifetime tokens',
      value: formatTokens(lifetimeTokens),
      hint: life ? `${formatCount(life.msgs)} msgs` : undefined,
    },
    { label: 'Lifetime cost', value: formatCost(lifetimeCost) },
    {
      label: 'Sessions',
      value: stats ? formatCount(stats.sessions.count) : '—',
      hint: stats ? `${formatCost(stats.sessions.avg_cost)}/avg` : undefined,
    },
    {
      label: 'Avg tokens/session',
      value: stats ? formatTokens(stats.sessions.avg_tokens) : '—',
    },
    {
      label: 'Cache hit ratio',
      value: stats ? formatPct(stats.cache_hit_ratio * 100) : '—',
      hint: 'of all tokens',
    },
    {
      label: 'Models · providers',
      value: stats ? `${stats.distinct_models} · ${stats.distinct_providers}` : '—',
    },
    {
      label: 'Busiest day',
      value: stats?.busiest_day ? stats.busiest_day.period_key : '—',
      hint: stats?.busiest_day ? formatTokens(stats.busiest_day.tokens) : undefined,
    },
    {
      label: 'Busiest hour',
      value: stats?.busiest_hour ? hourLabel(stats.busiest_hour.hour) : '—',
      hint: stats?.busiest_hour ? formatTokens(stats.busiest_hour.tokens) : undefined,
    },
  ];

  return (
    <section
      aria-label="Global usage insights"
      className="grid grid-cols-2 gap-3 md:grid-cols-4"
    >
      {tiles.map((tile) => (
        <StatTile
          key={tile.label}
          label={tile.label}
          value={tile.value}
          hint={tile.hint}
          loading={loading}
        />
      ))}
      {error ? (
        <div className="col-span-2 md:col-span-4">
          <EmptyState
            icon={CircleAlert}
            title="Could not load the global stats"
            description={error.message}
            action={
              onRetry ? (
                <Button size="sm" onClick={onRetry}>
                  Retry
                </Button>
              ) : null
            }
          />
        </div>
      ) : null}
    </section>
  );
}
