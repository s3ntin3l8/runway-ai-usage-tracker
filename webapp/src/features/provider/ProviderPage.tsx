// Provider detail — replaces the v1 modal with a deep-linkable route.
// /provider/:providerId?account=<account_id>; account defaults to the
// provider's first fleet entry.

import { useMemo } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, RefreshCw, RotateCcw } from 'lucide-react';
import { toast } from 'sonner';
import { collectProvider, resetProvider } from '@/api/endpoints';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { useFleet, useProviderConfigs } from '@/features/home/queries';
import { ActivityTab } from './ActivityTab';
import { CostTab } from './CostTab';
import { DebugTab } from './DebugTab';
import { EventsTab } from './EventsTab';
import { ForecastTab } from './ForecastTab';
import { OverviewTab } from './OverviewTab';
import { PeriodSelector } from './PeriodSelector';
import { SessionsBrowser } from './SessionsBrowser';
import { currentMonthKey, resolvePeriod } from './period';
import { useProviderEventRange } from './queries';

// Tabs whose data is scoped by the shared month selector.
const PERIOD_AWARE_TABS = new Set(['activity', 'sessions', 'events', 'cost']);

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

  // Shared month selector — `?period=YYYY-MM`, omitted when it's the current
  // month (mirrors how `tab` omits 'overview'). resolvePeriod tolerates a bad
  // deep-link by falling back to the current month.
  const period = resolvePeriod(searchParams.get('period'));
  const setPeriod = (next: string) => {
    setSearchParams(
      (prev) => {
        const p = new URLSearchParams(prev);
        if (next === currentMonthKey()) p.delete('period');
        else p.set('period', next);
        return p;
      },
      { replace: true },
    );
  };
  const eventRange = useProviderEventRange(providerId, accountId);

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

  return (
    <>
      <PageHeader
        title={name}
        description={entry?.critical_gauge.account_label || accountId}
        leading={<ProviderGlyph providerId={providerId} name={name} className="size-9 text-sm" />}
        actions={
          <>
            {entry && PERIOD_AWARE_TABS.has(tab) ? (
              <PeriodSelector
                value={period.key}
                onChange={setPeriod}
                earliest={eventRange.data?.earliest}
              />
            ) : null}
            {entries.length > 1 ? (
              <Select
                value={accountId}
                onValueChange={(v) =>
                setSearchParams(
                  (prev) => {
                    const p = new URLSearchParams(prev);
                    p.set('account', v);
                    return p;
                  },
                  { replace: true },
                )
              }
              >
                <SelectTrigger className="max-w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {entries.map((e) => (
                    <SelectItem key={e.account_id} value={e.account_id}>
                      {e.critical_gauge.account_label || e.account_id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label="Clear failure state"
              title="Clear failure state"
              onClick={() => reset.mutate()}
              loading={reset.isPending}
            >
              <RotateCcw className="size-3.5" aria-hidden />
            </Button>
            <Button
              size="sm"
              onClick={() => collect.mutate()}
              loading={collect.isPending}
              aria-label="Collect now"
            >
              <RefreshCw className="size-3.5" aria-hidden />
              <span className="hidden sm:inline">Collect</span>
            </Button>
          </>
        }
      />
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
              <OverviewTab entry={entry} />
            </TabsContent>
            <TabsContent value="activity">
              <ActivityTab providerId={providerId} accountId={accountId} period={period} />
            </TabsContent>
            <TabsContent value="sessions">
              <SessionsBrowser
                providerId={providerId}
                accountId={accountId}
                period={period}
                active={tab === 'sessions'}
              />
            </TabsContent>
            <TabsContent value="events">
              <EventsTab
                providerId={providerId}
                accountId={accountId}
                period={period}
                active={tab === 'events'}
              />
            </TabsContent>
            <TabsContent value="forecast">
              <ForecastTab providerId={providerId} accountId={accountId} entry={entry} />
            </TabsContent>
            <TabsContent value="cost">
              <CostTab providerId={providerId} accountId={accountId} period={period} />
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
