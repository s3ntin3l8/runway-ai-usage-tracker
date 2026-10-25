// Provider detail — replaces the v1 modal with a deep-linkable route.
// /provider/:providerId?account=<account_id>; account defaults to the
// provider's first fleet entry.

import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Archive, ArchiveRestore, Bug, CircleAlert, Ellipsis, RefreshCw, RotateCcw, SlidersHorizontal } from 'lucide-react';
import { toast } from 'sonner';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { collectProvider, putProviderConfigForAccount, resetProvider } from '@/api/endpoints';
import type { BlockedSource } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { EmptyState } from '@/components/ui/EmptyState';
import { PageHeader } from '@/components/layout/PageHeader';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { Skeleton, SkeletonRegion } from '@/components/ui/Skeleton';
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

// Why the provider has no data, when a machine reported a credential source it could
// not read: the store, the reason and the fix.
const blockedSourceDescription = (source: BlockedSource): string => {
  const store = source.source_store ?? source.credential_origin;
  const detail = source.detail ? ` (${source.detail})` : '';
  const remedy = source.remedy ? ` ${source.remedy}.` : '';
  return `${store} could not be read${detail}.${remedy}`;
};

// The tabs this page offers. A `?tab=` outside this list — a stale link, a
// hand-edited URL — used to leave the page blank: Radix renders a panel only
// for a matching value, so the header and the tab strip showed with no
// content, no selected tab and no way back.
const PROVIDER_TABS = [
  'overview',
  'activity',
  'sessions',
  'events',
  'forecast',
  'cost',
  'debug',
] as const;
type ProviderTab = (typeof PROVIDER_TABS)[number];

function isProviderTab(value: string | null | undefined): value is ProviderTab {
  return value != null && (PROVIDER_TABS as readonly string[]).includes(value);
}

export function ProviderPage() {
  const { providerId = '' } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const fleet = useFleet();
  const providerConfigs = useProviderConfigs();
  // Archive and reset are destructive: they ask first, naming the provider.
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const rawTab = searchParams.get('tab');
  const tab: ProviderTab = isProviderTab(rawTab) ? rawTab : 'overview';
  const setTab = (next: ProviderTab) => {
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
  // A credential source the sidecar could not read: the provider is not "not signed in",
  // its credential is simply unreadable, and the empty state should say so.
  const blockedSources = useMemo(
    () => (fleet.data?.blocked_sources ?? []).filter((s) => s.provider_id === providerId),
    [fleet.data, providerId],
  );
  const accountParam = searchParams.get('account');
  // The account was merged away: it holds nothing, so it has no entry here.
  // Saying where it went beats an unexplained "no data" — the operator
  // followed a link to an account that no longer exists.
  const mergedAway = useMemo(
    () =>
      (fleet.data?.merged_accounts ?? []).find(
        (m) => m.provider_id === providerId && m.account_id === accountParam,
      ),
    [fleet.data, providerId, accountParam],
  );
  // A merged-away account does not fall back to another account's entry: the
  // destination usually has one, and showing its data under the merged-away
  // account's name would be worse than saying where the account went.
  const entry = mergedAway
    ? undefined
    : (entries.find((e) => e.account_id === accountParam) ?? entries[0]);
  const accountId = entry?.account_id ?? accountParam ?? 'default';

  // Shared time-range picker — `?range=` holds 'Nd' or an absolute
  // 'YYYY-MM-DD_…' span, omitted for the default last-7-days window (mirrors
  // how `tab` omits 'overview'). The legacy `?period=` deep-link is still
  // honoured as a read-only fallback and cleared on the first change.
  const [rangeValue, setRange] = useRangeParam('period');
  const scope = resolveScope(rangeValue);
  const eventRange = useProviderEventRange(providerId, accountId);

  // A deep link to a tab that is off-screen — the strip scrolls on a phone,
  // and the last tabs are past the fold — must bring that tab into view,
  // or the link lands on a page that looks like the wrong tab is selected.
  useEffect(() => {
    const list = document.getElementById('provider-tabs');
    const trigger = list?.querySelector<HTMLElement>('[data-state="active"]');
    // jsdom has no scrollIntoView, hence the optional call.
    trigger?.scrollIntoView?.({ block: 'nearest', inline: 'center' });
    // The strip does not exist until the provider's entry has loaded, so the
    // effect has to wait for it: on a cold load `tab` is already its final
    // value and never changes again, and an effect that only watched `tab`
    // would have run once, against a strip that was not there yet.
  }, [tab, entry]);
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
    // The target state travels with the mutation instead of being read from
    // `isArchived` inside the callback: the toast's Undo has to send the
    // provider back to the state it was in, and a captured `isArchived` would
    // be stale by then.
    mutationFn: (archived: boolean) =>
      putProviderConfigForAccount(providerId, accountId, { archived }),
    onSuccess: (_data, archived) => {
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
      toast.success(archived ? 'Provider restored' : 'Provider archived', {
        action: {
          label: 'Undo',
          onClick: () => archive.mutate(!archived),
        },
      });
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
                <SelectTrigger className="max-w-44" label="Provider account">
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
                  <DropdownMenu.Item onSelect={() => setConfirmReset(true)} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-[13px] outline-none hover:bg-surface-2">
                    <RotateCcw className="size-3.5" aria-hidden /> Clear failure state
                  </DropdownMenu.Item>
                  <DropdownMenu.Item onSelect={() => setConfirmArchive(true)} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-[13px] outline-none hover:bg-surface-2">
                    {isArchived ? <ArchiveRestore className="size-3.5" aria-hidden /> : <Archive className="size-3.5" aria-hidden />}
                    {isArchived ? 'Restore provider' : 'Archive provider'}
                  </DropdownMenu.Item>
                  {/* The debug pane is a diagnostic surface, not a daily
                      destination — it stays out of the tab strip until it is
                      opened deliberately. */}
                  <DropdownMenu.Item onSelect={() => setTab('debug')} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-[13px] outline-none hover:bg-surface-2">
                    <Bug className="size-3.5" aria-hidden /> Debug
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
          // Goes Home, not back in history: on a bookmark, a deep link or a
          // refresh there is no in-app entry to go back to, and navigate(-1)
          // then leaves the dashboard entirely.
          onClick={() => navigate('/')}
          className="-ml-2 mb-3"
          aria-label="Back to Home"
        >
          <ArrowLeft className="size-3.5" aria-hidden /> Back
        </Button>

        {fleet.isPending ? (
          <div className="flex flex-col gap-3">
            <SkeletonRegion>
              <Skeleton className="h-10 w-full max-w-md" />
              <Skeleton className="h-64 w-full" />
            </SkeletonRegion>
          </div>
        ) : fleet.isError ? (
          // Before the empty branch: a failed request is not a missing
          // provider, and reporting "no data" for it sends the user looking
          // for a provider that is there.
          <EmptyState
            icon={CircleAlert}
            title="Could not load this provider"
            description={fleet.error.message}
            action={<Button size="sm" onClick={() => fleet.refetch()}>Retry</Button>}
          />
        ) : !entry ? (
          mergedAway ? (
            <EmptyState
              title="This account was merged away"
              description={`Its usage and credential moved to "${mergedAway.destination_account_id}".`}
              action={
                <Button
                  size="sm"
                  onClick={() =>
                    navigate(
                      `/provider/${providerId}?account=${encodeURIComponent(mergedAway.destination_account_id)}`,
                    )
                  }
                >
                  Go to the destination
                </Button>
              }
            />
          ) : (
            <EmptyState
              title={
                blockedSources.length > 0 ? 'Cookie store unavailable' : 'No data for this provider'
              }
              description={
                blockedSources.length > 0
                  ? blockedSourceDescription(blockedSources[0])
                  : `Nothing reported for "${providerId}" yet.`
              }
              action={
                <Button size="sm" onClick={() => collect.mutate()} loading={collect.isPending}>
                  Collect now
                </Button>
              }
            />
          )
        ) : (
          <Tabs value={tab} onValueChange={(next) => { if (isProviderTab(next)) setTab(next); }}>
            <TabsList id="provider-tabs">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="activity">Activity</TabsTrigger>
              <TabsTrigger value="sessions">Sessions</TabsTrigger>
              <TabsTrigger value="events">Events</TabsTrigger>
              <TabsTrigger value="forecast">Forecast</TabsTrigger>
              <TabsTrigger value="cost">Cost</TabsTrigger>
              {/* Debug is a diagnostic surface (a raw upstream capture), not a
                  daily destination — it stays out of the strip until it is
                  opened deliberately, and then it shows so the user can click
                  back out. */}
              {tab === 'debug' ? <TabsTrigger value="debug">Debug</TabsTrigger> : null}
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
      <ConfirmDialog
        open={confirmArchive}
        onOpenChange={setConfirmArchive}
        title={isArchived ? `Restore ${name}?` : `Archive ${name}?`}
        description={
          isArchived
            ? `${name} reports again on the dashboard and its usage is collected as before.`
            : `${name} stops reporting on the dashboard. Its usage history is kept, and you can restore it from Settings → Providers.`
        }
        confirmLabel={isArchived ? 'Restore' : 'Archive'}
        pending={archive.isPending}
        onConfirm={() => {
          setConfirmArchive(false);
          archive.mutate(!isArchived);
        }}
      />
      <ConfirmDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title="Clear the failure state?"
        description={`The next collection for ${name} starts from scratch. Usage that was already recorded is not changed.`}
        confirmLabel="Clear failure state"
        pending={reset.isPending}
        onConfirm={() => {
          setConfirmReset(false);
          reset.mutate();
        }}
      />
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
      trigger={<Button variant="secondary" size="sm" className="shrink-0"><SlidersHorizontal className="size-3.5" aria-hidden /> Filters{activeCount ? <span className="ml-1 rounded-full bg-accent/15 px-1.5 text-[10px] text-accent-text">{activeCount}</span> : null}</Button>}
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
