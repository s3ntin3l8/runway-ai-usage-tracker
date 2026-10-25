import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { Archive, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { putDashboardLayout } from '@/api/endpoints';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/Button';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { Skeleton } from '@/components/ui/Skeleton';
import { EmptyState } from '@/components/ui/EmptyState';
import { Inbox } from 'lucide-react';
import { useForceCollect } from '@/hooks/useForceCollect';
import { timeAgo } from '@/lib/format';
import { useSidecars } from '@/features/fleet/queries';
import { useDataHealthReport } from '@/features/settings/sections/dataHealth/queries';
import { AggregateStrip } from './AggregateStrip';
import { ArchivedSection } from './ArchivedSection';
import { AtRiskRail } from './AtRiskRail';
import { Banners } from './Banners';
import { MergedSection } from './MergedSection';
import { ProviderGrid } from './ProviderGrid';
import {
  useAnomalies,
  useCostForecast,
  useCumulative,
  useDashboardLayout,
  useFleet,
  useForecast,
  useProviderConfigs,
  useCredentialBanners,
} from './queries';
import { applyLayoutOrder, atRiskItems, buildRiskItems } from './risk';

export function HomePage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const fleet = useFleet();
  const forecast = useForecast();
  const cost = useCostForecast();
  const cumulative = useCumulative();
  const credentials = useCredentialBanners();
  const anomalies = useAnomalies();
  // 300s quiet-path cadence, matching useCredentialBanners — this is just the
  // error badge, not the Settings page's own report, so it doesn't need
  // the tighter default poll.
  const dataHealth = useDataHealthReport(300_000);
  const providerConfigs = useProviderConfigs();
  const sidecars = useSidecars();
  const layout = useDashboardLayout();
  const [showArchived, setShowArchived] = useState(false);

  const providerNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of providerConfigs.data?.providers ?? []) map.set(p.provider_id, p.name);
    return map;
  }, [providerConfigs.data]);

  // How many providers are actually set up. The settings list also contains
  // every provider the app knows about — most of them never configured — so
  // "the list is not empty" says nothing. An account counts as set up when it
  // carries a credential and is neither archived nor merged away.
  const configuredProviders = useMemo(() => {
    const providers = providerConfigs.data?.providers ?? [];
    return providers.filter((p) =>
      (p.accounts ?? []).some(
        (a) => !a.archived && !a.merged_into && (a.api_key_set || a.session_cookie_set),
      ),
    ).length;
  }, [providerConfigs.data]);

  // The billing type of the money the aggregate strip sums up. The accounts
  // here may mix billing types, so the aggregate takes the *least* certain
  // label of the set: reported money only while every account reports it,
  // estimated money as soon as one of them does not.
  const aggregateBillingType = useMemo(() => {
    const accounts = (providerConfigs.data?.providers ?? []).flatMap((p) => p.accounts ?? []);
    const live = accounts.filter(
      (a) => !a.archived && !a.merged_into && (a.api_key_set || a.session_cookie_set),
    );
    if (live.length === 0) return 'unknown';
    return live.every((a) => a.billing_type === 'pay_as_you_go')
      ? 'pay_as_you_go'
      : 'subscription';
  }, [providerConfigs.data]);

  // Machines that are already paired. A paired sidecar with no provider
  // account yet is a different situation from a fresh install, and the zero
  // state has to say so: telling someone to "connect a sidecar" they have
  // already connected sends them down a step they finished.
  const sidecarCount = sidecars.data?.sidecars?.length ?? 0;

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

  const collect = useForceCollect();

  const generatedAt = fleet.data?.generated_at;

  return (
    <>
      <PageHeader
        title="Home"
        description={generatedAt ? `updated ${timeAgo(generatedAt)}` : undefined}
        actions={
          <>
            <ExcludeCacheToggle compact />
            <Button
              size="sm"
              variant={showArchived ? 'primary' : 'ghost'}
              onClick={() => setShowArchived(!showArchived)}
              aria-label={showArchived ? 'Hide archived providers' : 'Show archived providers'}
            >
              <Archive className="size-3.5" aria-hidden />
              <span className="hidden sm:inline">Archived</span>
            </Button>
            <Button
              size="sm"
              onClick={() => collect.mutate()}
              loading={collect.isPending}
              aria-label="Collect now"
            >
              <RefreshCw className="size-3.5" aria-hidden />
              <span className="hidden sm:inline">Collect now</span>
            </Button>
          </>
        }
      />
      <div className="flex flex-col gap-5 p-4 lg:p-8">
        <Banners
          credentials={credentials.data}
          anomalies={anomalies.data?.anomalies}
          fleet={fleet.data?.fleet}
          dataHealth={dataHealth.data}
        />

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
          <HomeZeroState
            configuredProviders={configuredProviders}
            sidecarCount={sidecarCount}
            collecting={collect.isPending}
            onCollect={() => collect.mutate()}
            onAddProvider={() => navigate('/settings/providers')}
            onOpenFleet={() => navigate('/fleet')}
          />
        ) : (
          <>
            <AtRiskRail items={rail} providerNames={providerNames} />
            <AggregateStrip
              cost={cost.data}
              cumulative={cumulative.data}
              billingType={aggregateBillingType}
              costLoading={cost.isPending}
              tokensLoading={cumulative.isPending}
            />
            <ProviderGrid
              items={ordered}
              providerNames={providerNames}
              onReorder={(keys) => saveOrder.mutate(keys)}
            />
          </>
        )}
        {showArchived ? <ArchivedSection /> : null}
        <MergedSection />
      </div>
    </>
  );
}

/**
 * The zero state, in the three shapes it can take.
 *
 * A provider set up but silent is a different problem from a machine that
 * is paired but has no provider account yet, and both differ from a fresh
 * install. Each one names the step that is actually missing, so nobody is
 * told to connect a sidecar they already connected.
 */
function HomeZeroState({
  configuredProviders,
  sidecarCount,
  collecting,
  onCollect,
  onAddProvider,
  onOpenFleet,
}: {
  configuredProviders: number;
  sidecarCount: number;
  collecting: boolean;
  onCollect: () => void;
  onAddProvider: () => void;
  onOpenFleet: () => void;
}) {
  if (configuredProviders > 0) {
    return (
      <EmptyState
        icon={Inbox}
        title="Nothing reported yet"
        description={`${configuredProviders} provider${configuredProviders === 1 ? ' is' : 's are'} set up, but nothing has reported usage yet. Collect now, or check the sidecar.`}
        action={
          <>
            <Button size="sm" onClick={onCollect} loading={collecting}>
              Collect now
            </Button>
            <Button size="sm" variant="ghost" onClick={onAddProvider} className="ml-2">
              Provider settings
            </Button>
          </>
        }
      />
    );
  }

  if (sidecarCount > 0) {
    return (
      <EmptyState
        icon={Inbox}
        title="Sidecar connected, no provider yet"
        description={`${sidecarCount} machine${sidecarCount === 1 ? ' is' : 's are'} connected, but no provider account is set up yet. Add a provider to start tracking usage.`}
        action={
          <>
            <Button size="sm" onClick={onAddProvider}>
              Add a provider
            </Button>
            <Button size="sm" variant="ghost" onClick={onOpenFleet} className="ml-2">
              Manage sidecars
            </Button>
          </>
        }
      />
    );
  }

  return (
    <EmptyState
      icon={Inbox}
      title="No providers set up yet"
      description="Add a provider or connect a sidecar to start tracking usage."
      action={
        <>
          <Button size="sm" onClick={onAddProvider}>
            Add a provider
          </Button>
          <Button size="sm" variant="ghost" onClick={onOpenFleet} className="ml-2">
            Connect a sidecar
          </Button>
        </>
      }
    />
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
