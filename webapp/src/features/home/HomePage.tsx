import { useMemo } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { forceCollect, putDashboardLayout } from '@/api/endpoints';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/Button';
import { Skeleton } from '@/components/ui/Skeleton';
import { EmptyState } from '@/components/ui/EmptyState';
import { Inbox } from 'lucide-react';
import { timeAgo } from '@/lib/format';
import { AggregateStrip } from './AggregateStrip';
import { AtRiskRail } from './AtRiskRail';
import { Banners } from './Banners';
import { ProviderGrid } from './ProviderGrid';
import {
  useAnomalies,
  useCostForecast,
  useCumulative,
  useDashboardLayout,
  useFleet,
  useForecast,
  useProviderConfigs,
  useTokenHealth,
} from './queries';
import { applyLayoutOrder, atRiskItems, buildRiskItems } from './risk';

export function HomePage() {
  const queryClient = useQueryClient();
  const fleet = useFleet();
  const forecast = useForecast();
  const cost = useCostForecast();
  const cumulative = useCumulative();
  const tokenHealth = useTokenHealth();
  const anomalies = useAnomalies();
  const providerConfigs = useProviderConfigs();
  const layout = useDashboardLayout();

  const providerNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of providerConfigs.data?.providers ?? []) map.set(p.provider_id, p.name);
    return map;
  }, [providerConfigs.data]);

  const items = useMemo(
    () => buildRiskItems(fleet.data?.fleet ?? [], forecast.data?.forecasts ?? []),
    [fleet.data, forecast.data],
  );
  const rail = useMemo(() => atRiskItems(items), [items]);
  const ordered = useMemo(
    () => applyLayoutOrder(items, layout.data?.provider_order),
    [items, layout.data],
  );

  const saveOrder = useMutation({
    mutationFn: (orderedKeys: string[]) =>
      putDashboardLayout({
        provider_order: orderedKeys,
        card_orders: layout.data?.card_orders ?? {},
      }),
    onMutate: async (orderedKeys) => {
      // Optimistic: the grid order is exactly what the user just dropped.
      await queryClient.cancelQueries({ queryKey: ['system', 'dashboard-layout'] });
      queryClient.setQueryData(['system', 'dashboard-layout'], {
        provider_order: orderedKeys,
        card_orders: layout.data?.card_orders ?? {},
      });
    },
    onError: () => {
      toast.error('Could not save the layout');
      queryClient.invalidateQueries({ queryKey: ['system', 'dashboard-layout'] });
    },
  });

  const collect = useMutation({
    mutationFn: forceCollect,
    onSuccess: (result) => {
      toast.success(
        `Collection triggered — ${result.cards} cards, ${result.sidecars_triggered} sidecars`,
      );
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
    onError: (err) => toast.error(`Collection failed: ${err.message}`),
  });

  const generatedAt = fleet.data?.generated_at;

  return (
    <>
      <PageHeader
        title="Home"
        description={generatedAt ? `updated ${timeAgo(generatedAt)}` : undefined}
        actions={
          <Button
            size="sm"
            onClick={() => collect.mutate()}
            loading={collect.isPending}
            aria-label="Collect now"
          >
            <RefreshCw className="size-3.5" aria-hidden />
            <span className="hidden sm:inline">Collect now</span>
          </Button>
        }
      />
      <div className="flex flex-col gap-5 p-4 lg:p-8">
        <Banners tokens={tokenHealth.data?.tokens} anomalies={anomalies.data?.anomalies} />

        {fleet.isPending ? (
          <HomeSkeleton />
        ) : fleet.isError ? (
          <EmptyState
            icon={Inbox}
            title="Could not load usage data"
            description={fleet.error.message}
            action={
              <Button size="sm" onClick={() => fleet.refetch()}>
                Retry
              </Button>
            }
          />
        ) : items.length === 0 ? (
          <EmptyState
            icon={Inbox}
            title="No providers reporting yet"
            description="Configure a provider in Settings or connect a sidecar to start tracking usage."
          />
        ) : (
          <>
            <AtRiskRail items={rail} providerNames={providerNames} />
            <AggregateStrip
              cost={cost.data}
              cumulative={cumulative.data}
              loading={cost.isPending || cumulative.isPending}
            />
            <ProviderGrid
              items={ordered}
              providerNames={providerNames}
              onReorder={(keys) => saveOrder.mutate(keys)}
            />
          </>
        )}
      </div>
    </>
  );
}

function HomeSkeleton() {
  return (
    <div className="flex flex-col gap-5">
      <Skeleton className="h-12 w-full" />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-[72px]" />
        ))}
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
        {Array.from({ length: 6 }, (_, i) => (
          <Skeleton key={i} className="h-36" />
        ))}
      </div>
    </div>
  );
}
