// Provider detail — replaces the v1 modal with a deep-linkable route.
// /provider/:providerId?account=<account_id>; account defaults to the
// provider's first fleet entry.

import { useMemo } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Archive, ArchiveRestore, Ellipsis, RefreshCw, RotateCcw, SlidersHorizontal } from 'lucide-react';
import { toast } from 'sonner';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { collectProvider, putProviderConfigForAccount, resetProvider } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { PageHeader } from '@/components/layout/PageHeader';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { TimeRangePicker } from '@/components/ui/TimeRangePicker';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { SidecarFilter } from '@/components/ui/SidecarFilter';
import { Popover } from '@/components/ui/Popover';
import { useFleet, useProviderConfigs } from '@/features/home/queries';
import { useRangeParam } from '@/hooks/useRangeParam';
import { cardKind } from '@/lib/quota';
import { ActivityTab } from './ActivityTab';
import { CostTab } from './CostTab';
import { DebugTab } from './DebugTab';
import { EventsTab } from './EventsTab';
import { ForecastTab } from './ForecastTab';
import { OverviewTab } from './OverviewTab';
import { SessionsBrowser } from './SessionsBrowser';
import { resolveScope } from './period';
import { useProviderEventRange } from './queries';
import { labelOrMaskedId } from '@/lib/accountDisplay';
import { useUsageSource } from '@/hooks/useUsageSource';
import { useExcludeCache } from '@/hooks/useExcludeCache';

// Tabs whose data is scoped by the shared time-range picker.
const PERIOD_AWARE_TABS = new Set(['overview', 'activity', 'sessions', 'events', 'cost']);
const SOURCE_AWARE_TABS = new Set(['overview', 'activity', 'sessions', 'events', 'cost']);
const CACHE_AWARE_TABS = new Set(['overview', 'activity', 'sessions', 'forecast', 'cost']);

export function ProviderPage() {
  const { providerId = '' } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const fleet = useFleet();
  const providerConfigs = useProviderConfigs();
  const tab = searchParams.get('tab') ?? 'overview';
  const setTab = (next: string) => {
    setSearchParams(
      (prev) => {
        const p = new URLSearchParams(prev);
        if (next === 'overview') p.delete('tab');
        else p.set('tab', next);
        return p;
      },
      { replace: true },
    );
  };

  const entries = useMemo(
    () => (fleet.data?.fleet ?? []).filter((e) => e.provider_id === providerId),
    [fleet.data, providerId],
  );
  const accountParam = searchParams.get('account');
  const entry = entries.find((e) => e.account_id === accountParam) ?? entries[0];
  const accountId = entry?.account_id ?? accountParam ?? 'default';

  // Shared time-range picker — `?range=` holds 'Nd' or an absolute
  // 'YYYY-MM-DD_…' span, omitted for the default last-7-days window (mirrors
  // how `tab` omits 'overview'). The legacy `?period=` deep-link is still
  // honoured as a read-only fallback and cleared on the first change.
  const [rangeValue, setRange] = useRangeParam('period');
  const scope = resolveScope(rangeValue);
  const eventRange = useProviderEventRange(providerId, accountId);
  const showSourceFilter =
    entry &&
    (SOURCE_AWARE_TABS.has(tab) || (tab === 'forecast' && cardKind(entry.critical_gauge) === 'tokens'));

  const name =
    providerConfigs.data?.providers.find((p) => p.provider_id === providerId)?.name ?? providerId;

  const collect = useMutation({
    mutationFn: () => collectProvider(providerId, accountId),
    onSuccess: () => {
      toast.success('Collection triggered');
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
    onError: (err) => toast.error(`Collect failed: ${err.message}`),
  });

  const reset = useMutation({
    mutationFn: () => resetProvider(providerId, accountId),
    onSuccess: () => {
      toast.success('Failure state cleared');
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
    onError: (err) => toast.error(`Reset failed: ${err.message}`),
  });

  const isArchived =
    providerConfigs.data?.providers
      .find((p) => p.provider_id === providerId)
      ?.accounts?.find((a) => a.account_id === accountId)?.archived
    ?? providerConfigs.data?.providers.find((p) => p.provider_id === providerId)?.archived
    ?? false;

  const archive = useMutation({
    mutationFn: () => putProviderConfigForAccount(providerId, accountId, { archived: !isArchived }),
    onSuccess: () => {
      toast.success(isArchived ? 'Provider restored' : 'Provider archived');
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
      if (!isArchived) navigate('/', { replace: true });
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <>
      <PageHeader
        sticky
        className="h-auto flex-wrap items-center justify-start gap-x-3 gap-y-2 bg-surface-1/95 pb-2 md:h-14 md:flex-nowrap md:gap-3 md:pb-0 md:pt-0"
      >
        <div className="order-1 flex min-w-0 flex-1 items-center gap-2">
          <ProviderGlyph providerId={providerId} name={name} className="size-9 shrink-0 text-sm" />
          <div className="min-w-0">
            <h1 className="truncate text-[14px] font-medium">{name}</h1>
            <div className="truncate text-[11px] text-fg-muted">{labelOrMaskedId({ account_id: accountId ?? '', account_label: entry?.critical_gauge.account_label })}</div>
          </div>
        </div>
        <div className="order-3 flex min-w-0 w-full flex-wrap items-center gap-2 md:order-2 md:w-auto md:flex-nowrap">
            {entry && PERIOD_AWARE_TABS.has(tab) ? (
              <TimeRangePicker
                value={rangeValue}
                onChange={setRange}
                earliest={eventRange.data?.earliest}
              />
            ) : null}
            {(showSourceFilter || (entry && CACHE_AWARE_TABS.has(tab))) ? (
              <ProviderFilters showSource={Boolean(showSourceFilter)} showCache={Boolean(entry && CACHE_AWARE_TABS.has(tab))} />
            ) : null}
            {entries.length > 1 ? (
              <Select
                value={accountId}
                onValueChange={(v) => setSearchParams((prev) => {
                  const p = new URLSearchParams(prev);
                  p.set('account', v);
                  return p;
                }, { replace: true })}
              >
                <SelectTrigger className="max-w-44" aria-label="Provider account">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {entries.map((e) => (
                    <SelectItem key={e.account_id} value={e.account_id}>
                      {labelOrMaskedId({ account_id: e.account_id, account_label: e.critical_gauge.account_label })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
        </div>
        <div className="order-2 ml-auto flex items-center justify-end gap-1 md:order-3">
            <Button
              size="sm"
              onClick={() => collect.mutate()}
              loading={collect.isPending}
              aria-label="Collect now"
            >
              <RefreshCw className="size-3.5" aria-hidden />
              <span className="hidden md:inline">Collect</span>
            </Button>
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild>
                <Button size="icon-sm" variant="ghost" aria-label="More provider actions"><Ellipsis className="size-4" aria-hidden /></Button>
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content align="end" sideOffset={6} className="z-50 min-w-48 rounded-lg border border-edge bg-overlay p-1 shadow-md">
                  <DropdownMenu.Item onSelect={() => reset.mutate()} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-[13px] outline-none hover:bg-surface-2">
                    <RotateCcw className="size-3.5" aria-hidden /> Clear failure state
                  </DropdownMenu.Item>
                  <DropdownMenu.Item onSelect={() => archive.mutate()} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-[13px] outline-none hover:bg-surface-2">
                    {isArchived ? <ArchiveRestore className="size-3.5" aria-hidden /> : <Archive className="size-3.5" aria-hidden />}
                    {isArchived ? 'Restore provider' : 'Archive provider'}
                  </DropdownMenu.Item>
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
        </div>
      </PageHeader>
      <div className="px-4 pt-3 pb-4 lg:px-8 lg:pt-4 lg:pb-8">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate(-1)}
          className="-ml-2 mb-3"
          aria-label="Back"
        >
          <ArrowLeft className="size-3.5" aria-hidden /> Back
        </Button>

        {fleet.isPending ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-10 w-full max-w-md" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : !entry ? (
          <EmptyState
            title="No data for this provider"
            description={`Nothing reported for "${providerId}" yet.`}
            action={
              <Button size="sm" onClick={() => collect.mutate()} loading={collect.isPending}>
                Collect now
              </Button>
            }
          />
        ) : (
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList>
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="activity">Activity</TabsTrigger>
              <TabsTrigger value="sessions">Sessions</TabsTrigger>
              <TabsTrigger value="events">Events</TabsTrigger>
              <TabsTrigger value="forecast">Forecast</TabsTrigger>
              <TabsTrigger value="cost">Cost</TabsTrigger>
              <TabsTrigger value="debug">Debug</TabsTrigger>
            </TabsList>
            <TabsContent value="overview">
              <OverviewTab entry={entry} scope={scope} />
            </TabsContent>
            <TabsContent value="activity">
              <ActivityTab providerId={providerId} accountId={accountId} scope={scope} />
            </TabsContent>
            <TabsContent value="sessions">
              <SessionsBrowser
                providerId={providerId}
                accountId={accountId}
                scope={scope}
                active={tab === 'sessions'}
              />
            </TabsContent>
            <TabsContent value="events">
              <EventsTab
                providerId={providerId}
                accountId={accountId}
                scope={scope}
                active={tab === 'events'}
              />
            </TabsContent>
            <TabsContent value="forecast">
              <ForecastTab providerId={providerId} accountId={accountId} entry={entry} />
            </TabsContent>
            <TabsContent value="cost">
              <CostTab
                providerId={providerId}
                accountId={accountId}
                scope={scope}
                billingType={
                  providerConfigs.data?.providers
                    .find((p) => p.provider_id === providerId)
                    ?.accounts?.find((a) => a.account_id === accountId)?.billing_type ?? 'unknown'
                }
              />
            </TabsContent>
            <TabsContent value="debug">
              <DebugTab
                providerId={providerId}
                accountId={accountId}
                entry={entry}
                active={tab === 'debug'}
              />
            </TabsContent>
          </Tabs>
        )}
      </div>
    </>
  );
}

function ProviderFilters({ showSource, showCache }: { showSource: boolean; showCache: boolean }) {
  const [sidecarId] = useUsageSource();
  const { excludeCache } = useExcludeCache();
  const filters = [
    { visible: showSource, active: Boolean(sidecarId) },
    { visible: showCache, active: excludeCache },
  ];
  // Count only exposed filters so the badge stays aligned as controls are added.
  const activeCount = filters.reduce((count, filter) => count + Number(filter.visible && filter.active), 0);
  return (
    <Popover
      align="start"
      className="w-72 space-y-4"
      trigger={<Button variant="secondary" size="sm" className="shrink-0"><SlidersHorizontal className="size-3.5" aria-hidden /> Filters{activeCount ? <span className="ml-1 rounded-full bg-accent/15 px-1.5 text-[10px] text-accent">{activeCount}</span> : null}</Button>}
    >
      <div className="space-y-1">
        <h3 className="text-[12px] font-medium">Provider filters</h3>
        <p className="text-[11px] text-fg-muted">Filters apply to the current tab where supported.</p>
      </div>
      {showSource ? <div className="space-y-1"><div className="text-[11px] text-fg-muted">Source</div><SidecarFilter /></div> : null}
      {showCache ? <div className="space-y-1"><div className="text-[11px] text-fg-muted">Cached tokens</div><ExcludeCacheToggle /></div> : null}
    </Popover>
  );
}
