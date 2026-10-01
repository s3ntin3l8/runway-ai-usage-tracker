// Insights: cross-provider aggregates — lifetime global stats plus the Top
// Models / Projects / Tools rankings over the shared time range. Unlike
// History, nothing here keys off a single account.

import { PageHeader } from '@/components/layout/PageHeader';
import { SidecarFilter } from '@/components/ui/SidecarFilter';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { TimeRangePicker } from '@/components/ui/TimeRangePicker';
import { useRangeParam } from '@/hooks/useRangeParam';
import { GlobalInsights } from './GlobalInsights';
import { OverallChartCard } from './OverallChartCard';
import { TopModelsCard } from './TopModelsCard';
import { TopProjectsCard } from './TopProjectsCard';
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
        <div className="flex flex-col gap-3">
          <h2 className="text-[13px] font-semibold tracking-tight">Global insights · All time</h2>
          <GlobalInsights stats={globalStats.data} loading={globalStats.isPending} />
        </div>

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <h2 className="text-[13px] font-semibold tracking-tight">Over time</h2>
        </div>

        <OverallChartCard range={range} />

        <TopModelsCard range={range} />

        <div className="grid gap-4 lg:grid-cols-2">
          <TopProjectsCard range={range} />
          <TopToolsCard range={range} />
        </div>
      </div>
    </>
  );
}
