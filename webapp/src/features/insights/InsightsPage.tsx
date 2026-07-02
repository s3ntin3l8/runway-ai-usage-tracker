// Insights: cross-provider aggregates — lifetime global stats plus the Top
// Models / Projects / Tools rankings over a selectable day-range. Unlike
// History, nothing here keys off a single account.

import { useState } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { GlobalInsights } from './GlobalInsights';
import { OverallChartCard } from './OverallChartCard';
import { TopModelsCard } from './TopModelsCard';
import { TopProjectsCard } from './TopProjectsCard';
import { TopToolsCard } from './TopToolsCard';
import { useGlobalStats } from './queries';

const RANGES = [
  { days: 7, label: '7d' },
  { days: 14, label: '14d' },
  { days: 30, label: '30d' },
  { days: 90, label: '90d' },
];

export function InsightsPage() {
  const [days, setDays] = useState(7);
  const globalStats = useGlobalStats();

  return (
    <>
      <PageHeader title="Insights" description="Cross-provider usage" />
      <div className="flex flex-col gap-4 p-4 lg:p-8">
        <div className="flex items-center justify-end">
          <ExcludeCacheToggle />
        </div>

        <div className="flex flex-col gap-3">
          <h2 className="text-[13px] font-semibold tracking-tight">Global insights · All time</h2>
          <GlobalInsights stats={globalStats.data} loading={globalStats.isPending} />
        </div>

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <h2 className="text-[13px] font-semibold tracking-tight">Over time</h2>
          <Tabs
            value={String(days)}
            onValueChange={(v) => setDays(Number(v))}
            className="ml-auto"
          >
            <TabsList className="border-0">
              {RANGES.map((r) => (
                <TabsTrigger key={r.days} value={String(r.days)} className="h-9 px-2.5">
                  {r.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>

        <OverallChartCard days={days} />

        <TopModelsCard days={days} />

        <div className="grid gap-4 lg:grid-cols-2">
          <TopProjectsCard days={days} />
          <TopToolsCard days={days} />
        </div>
      </div>
    </>
  );
}
