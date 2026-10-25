// Insights: cross-provider aggregates — lifetime global stats plus the Top
// Models / Projects / Tools / Sessions / Hosts rankings over the shared time
// range. Unlike History, nothing here keys off a single account.

import { PageHeader } from '@/components/layout/PageHeader';
import { SidecarFilter } from '@/components/ui/SidecarFilter';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { TimeRangePicker } from '@/components/ui/TimeRangePicker';
import { useRangeParam } from '@/hooks/useRangeParam';
import { formatRangeLabel } from '@/lib/timeRange';
import { GlobalInsights } from './GlobalInsights';
import { OverallChartCard } from './OverallChartCard';
import { TopHostsCard } from './TopHostsCard';
import { TopModelsCard } from './TopModelsCard';
import { TopProjectsCard } from './TopProjectsCard';
import { TopSessionsCard } from './TopSessionsCard';
import { TopToolsCard } from './TopToolsCard';
import { useGlobalStats } from './queries';

export function InsightsPage() {
  const [range, setRange] = useRangeParam();
  const globalStats = useGlobalStats();

  return (
    <>
      <PageHeader
        title="Insights"
        description="Cross-provider usage"
        actions={
          <>
            <SidecarFilter />
            <TimeRangePicker value={range} onChange={setRange} />
            <ExcludeCacheToggle compact />
          </>
        }
      />
      <div className="flex flex-col gap-4 p-4 lg:p-8">
        {/*
          The range picker in the header applies to everything BELOW this
          block — these are lifetime totals. The scope used to be a trailing
          "· All time" on a heading the size of the other labels, which read
          as if the whole page were scoped one way; it is stated next to the
          numbers and the block is visually separated.
        */}
        <section
          aria-label="All-time totals"
          className="flex flex-col gap-3 rounded-lg border border-edge bg-surface-1 p-4"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-[13px] font-semibold tracking-tight">Global insights</h2>
            <span className="text-[11px] text-fg-subtle">
              All time — not affected by the range filter
            </span>
          </div>
          <GlobalInsights
            stats={globalStats.data}
            loading={globalStats.isPending}
            error={globalStats.isError ? globalStats.error : null}
            onRetry={() => globalStats.refetch()}
          />
        </section>

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <h2 className="text-[13px] font-semibold tracking-tight">
            Over time · {formatRangeLabel(range)}
          </h2>
        </div>

        <OverallChartCard range={range} />

        <TopModelsCard range={range} />

        <div className="grid gap-4 lg:grid-cols-2">
          <TopProjectsCard range={range} />
          <TopToolsCard range={range} />
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <TopSessionsCard range={range} />
          <TopHostsCard range={range} />
        </div>
      </div>
    </>
  );
}
