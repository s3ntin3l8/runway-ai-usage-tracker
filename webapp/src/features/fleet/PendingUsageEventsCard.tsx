import { useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  assignPendingUsageEvents,
  assignPendingUsageEventsBatch,
  fetchPendingUsageSessions,
  fetchProviderConfigs,
} from '@/api/endpoints';
import { Card } from '@/components/ui/Card';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import type {
  PendingUsageAssignmentGroup,
  PendingUsageFilter,
  PendingUsageSession,
  ProviderConfig,
} from '@/api/types';
import { accountConfigProviderIdForUsage, relatedAccountProviderIds } from '@/lib/providerAccountAliases';
import { AddProviderWizard } from '@/features/settings/sections/AddProviderWizard';
import { labelOrMaskedId } from '@/lib/accountDisplay';
import { errorMessage } from '@/lib/errorMessage';
import { buildSidecarNameMap, useSidecars } from './queries';
import { assignBatchInChunks, type BatchAssignmentOutcome } from './pendingUsageBatch';
import {
  decodeAssignTarget,
  encodeAssignTarget,
  sessionKey,
  type AssignOption,
  type AssignOptionGroup,
  type AssignOptionKind,
} from './pendingUsageSession';
import { PendingUsageBatchAssign } from './PendingUsageBatchAssign';
import {
  PendingUsageFilters,
  PendingUsagePagination,
  PendingUsageSummary,
} from './PendingUsageEventsChrome';
import { PendingUsageSessionsTable } from './PendingUsageSessionsTable';

export function PendingUsageEventsCard() {
  const queryClient = useQueryClient();
  const machineNames = buildSidecarNameMap(useSidecars().data?.sidecars ?? []);
  const [offset, setOffset] = useState(0);
  const [host, setHost] = useState('');
  const [provider, setProvider] = useState('');
  const [search, setSearch] = useState('');
  const [accounts, setAccounts] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<Record<string, PendingUsageSession>>({});
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchAccounts, setBatchAccounts] = useState<Record<string, string>>({});
  const [selectingAll, setSelectingAll] = useState(false);
  const selectionRequestId = useRef(0);
  const filters = useMemo<PendingUsageFilter>(
    () => ({ sidecar_id: host || undefined, provider_id: provider || undefined, search: search || undefined }),
    [host, provider, search],
  );
  const pending = useQuery({
    queryKey: ['fleet', 'pending_usage_sessions', offset, filters],
    queryFn: () => fetchPendingUsageSessions({ offset, filters }),
    refetchInterval: 60_000,
  });
  const configs = useQuery({
    queryKey: ['system', 'provider-configs'],
    queryFn: fetchProviderConfigs,
  });
  const assign = useMutation({
    mutationFn: ({
      eventIds,
      accountId,
      targetProviderId,
    }: {
      eventIds: number[];
      accountId: string;
      targetProviderId?: string;
      sidecarId: string;
    }) =>
      targetProviderId
        ? assignPendingUsageEvents(eventIds, accountId, targetProviderId)
        : assignPendingUsageEvents(eventIds, accountId),
    onSuccess: ({ assigned, provider_id, target_provider_id }, variables) => {
      const redirected = target_provider_id && target_provider_id !== accountConfigProviderIdForUsage(provider_id);
      toast.success(
        `${assigned} usage event${assigned === 1 ? '' : 's'} assigned${redirected ? ` to ${providerName(target_provider_id)}` : ''}. Future ${provider_id} events from ${variables.sidecarId} will use the selected account after its next config sync.`,
      );
      const assignedIds = new Set(variables.eventIds);
      setSelected((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([, group]) => !group.event_ids.some((id) => assignedIds.has(id))),
        ),
      );
      setOffset(0);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_sessions'] });
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_events'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
    onError: (error) => toast.error(error.message),
  });
  const batch = useMutation<
    BatchAssignmentOutcome,
    Error,
    PendingUsageAssignmentGroup[]
  >({
    mutationFn: (assignments) => assignBatchInChunks(assignments, assignPendingUsageEventsBatch),
    onSuccess: (outcome) => {
      const confirmed = new Set(outcome.assignedEventIds);
      // A chunk can split a session group, so the assigned events leave the
      // selection while the ones that are still pending stay in it.
      setSelected((current) =>
        Object.fromEntries(
          Object.entries(current)
            .map(([key, group]): [string, PendingUsageSession] => {
              const remaining = group.event_ids.filter((id) => !confirmed.has(id));
              return [key, { ...group, event_ids: remaining, event_count: remaining.length }];
            })
            .filter(([, group]) => group.event_ids.length > 0),
        ),
      );
      setOffset(0);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_sessions'] });
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_events'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
      if (outcome.error !== undefined) {
        // The chunk that failed left its events pending, and the chunks
        // after it were never sent. They stay selected, so a retry assigns
        // exactly what is left.
        toast.error(
          `${outcome.unassignedEventIds.length} usage event${outcome.unassignedEventIds.length === 1 ? '' : 's'} not assigned: ${errorMessage(outcome.error, 'the request failed')}. They are still selected — assign again to retry.`,
        );
        return;
      }
      toast.success(
        `${outcome.assigned} usage event${outcome.assigned === 1 ? '' : 's'} assigned. Future attribution updated for ${outcome.mappings.length} provider/host pair${outcome.mappings.length === 1 ? '' : 's'} after the next config sync.`,
      );
      setSelected({});
      setBatchAccounts({});
      setBatchOpen(false);
    },
    onError: (error) => toast.error(error.message),
  });

  const updateFilter = (setter: (value: string) => void) => (value: string) => {
    selectionRequestId.current += 1;
    setSelectingAll(false);
    setter(value);
    setOffset(0);
    setSelected({});
  };
  const selectedGroups = Object.values(selected);
  const selectedEventCount = selectedGroups.reduce((sum, group) => sum + group.event_count, 0);
  const selectedProviders = [...new Set(selectedGroups.map((group) => group.provider_id))].sort();
  // Account choices for a usage provider: its own active accounts, its archived
  // ones (the backend accepts them), and accounts of related providers (e.g.
  // Antigravity for Gemini). Disabled-but-not-archived accounts stay hidden.
  const optionGroupsForProvider = (usageProviderId: string): AssignOptionGroup[] => {
    const configProviderId = accountConfigProviderIdForUsage(usageProviderId);
    const toOption = (item: ProviderConfig, account: ProviderConfig['accounts'][number], kind: AssignOptionKind) => {
      const label = labelOrMaskedId(account);
      return {
        value: encodeAssignTarget(item.provider_id, account.account_id),
        providerId: item.provider_id,
        accountId: account.account_id,
        label: account.account_id === 'default' ? `${label} (default)` : label,
        kind,
      };
    };
    const own = configs.data?.providers.find((item) => item.provider_id === configProviderId);
    const groups: AssignOptionGroup[] = [];
    if (own) {
      groups.push({
        label: own.name || own.provider_id,
        options: own.accounts
          .filter((account) => !account.archived && account.enabled !== false)
          .map((account) => toOption(own, account, 'active')),
      });
      groups.push({
        label: `${own.name || own.provider_id} (archived)`,
        options: own.accounts
          .filter((account) => account.archived)
          .map((account) => toOption(own, account, 'archived')),
      });
    }
    for (const relatedId of relatedAccountProviderIds(configProviderId)) {
      const related = configs.data?.providers.find((item) => item.provider_id === relatedId);
      if (!related) continue;
      groups.push({
        label: related.name || related.provider_id,
        options: related.accounts
          .filter((account) => !account.archived && account.enabled !== false)
          .map((account) => toOption(related, account, 'related')),
      });
    }
    return groups.filter((group) => group.options.length > 0);
  };
  const optionsForProvider = (usageProviderId: string) =>
    optionGroupsForProvider(usageProviderId).flatMap((group) => group.options);
  const findOption = (usageProviderId: string, value: string | undefined) =>
    value ? optionsForProvider(usageProviderId).find((option) => option.value === value) : undefined;
  // A stored choice only counts while it is still offered (the account may have
  // been archived or disabled elsewhere while the card was open). A just-saved
  // account becomes valid again once the configs refetch lands.
  const validChoice = (usageProviderId: string, value: string | undefined) =>
    findOption(usageProviderId, value) ? value : undefined;
  // Only send a target when the account isn't the event provider's own config provider.
  const targetProviderFor = (usageProviderId: string, value: string) => {
    const { providerId } = decodeAssignTarget(value);
    return providerId === accountConfigProviderIdForUsage(usageProviderId) ? undefined : providerId;
  };
  const optionNote = (usageProviderId: string, option: AssignOption | undefined) => {
    if (option?.kind === 'archived') {
      return `Usage will be stored on this archived account: hidden from Fleet, still counted in Stats and archived lifetime stats. Future default-identity ${usageProviderId} events from this host go there too.`;
    }
    if (option?.kind === 'related') {
      return `These ${usageProviderId} events will be counted as ${providerName(option.providerId)} usage on this account. Future ${usageProviderId} events from this host follow after the next config sync.`;
    }
    return null;
  };

  const [wizardTarget, setWizardTarget] = useState<{
    provider: ProviderConfig | null;
    providerId: string;
    rowKey?: string;
  } | null>(null);

  const existingAccountIdsByProvider = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const p of configs.data?.providers ?? []) {
      map.set(
        p.provider_id,
        new Set(p.accounts.map((a) => a.account_id)),
      );
    }
    return map;
  }, [configs.data?.providers]);

  const providerName = (providerId: string) => {
    const configProviderId = accountConfigProviderIdForUsage(providerId);
    const found = configs.data?.providers.find((item) => item.provider_id === configProviderId);
    return found?.name || providerId;
  };

  const openWizard = (providerId: string, rowKey?: string) => {
    const configProviderId = accountConfigProviderIdForUsage(providerId);
    const found = configs.data?.providers.find((item) => item.provider_id === configProviderId) ?? null;
    setWizardTarget({
      provider: found,
      providerId,
      rowKey,
    });
  };

  async function selectAllMatching() {
    if (!pending.data || !host) return;
    const requestId = ++selectionRequestId.current;
    const selectedFilters = { ...filters };
    setSelectingAll(true);
    try {
      const first = await fetchPendingUsageSessions({ offset: 0, filters: selectedFilters, limit: 500 });
      if (selectionRequestId.current !== requestId) return;
      const all = [...first.items];
      for (let pageOffset = 500; pageOffset < first.total_groups; pageOffset += 500) {
        const page = await fetchPendingUsageSessions({ offset: pageOffset, filters: selectedFilters, limit: 500 });
        if (selectionRequestId.current !== requestId) return;
        all.push(...page.items);
      }
      if (selectionRequestId.current !== requestId) return;
      setSelected(Object.fromEntries(all.map((group) => [sessionKey(group), group])));
    } catch (error) {
      toast.error(errorMessage(error, 'Could not select matching usage groups'));
    } finally {
      if (selectionRequestId.current === requestId) setSelectingAll(false);
    }
  }

  function openBatch() {
    setBatchAccounts({});
    setBatchOpen(true);
  }

  function applyBatch() {
    const byProvider = new Map<string, number[]>();
    for (const group of selectedGroups) {
      byProvider.set(group.provider_id, [...(byProvider.get(group.provider_id) ?? []), ...group.event_ids]);
    }
    if ([...byProvider.keys()].some((providerId) => !validChoice(providerId, batchAccounts[providerId]))) return;
    const assignments: PendingUsageAssignmentGroup[] = [...byProvider.entries()].map(([providerId, eventIds]) => {
      const value = batchAccounts[providerId]!;
      const targetProviderId = targetProviderFor(providerId, value);
      return {
        event_ids: eventIds,
        account_id: decodeAssignTarget(value).accountId,
        ...(targetProviderId ? { target_provider_id: targetProviderId } : {}),
      };
    });
    batch.mutate(assignments);
  }

  if (pending.isPending) return null;
  if (pending.isError) {
    return (
      <Card className="mb-3 border-warning/40 bg-warning-muted p-3">
        <h2 className="text-sm font-semibold">Unassigned usage</h2>
        <p className="mt-1 text-xs text-fg-muted">Could not load unassigned usage events. Try refreshing the page.</p>
      </Card>
    );
  }
  if (!pending.data?.sidecars.length && !pending.data?.providers.length && !pending.data?.total_events) return null;

  const hasDefaultKeyedOption = (configs.data?.providers ?? []).some((item) =>
    item.accounts.some(
      (account) => account.account_id === 'default' && !account.archived && account.enabled !== false,
    ),
  );

  return (
    <Card id="pending-events" className="mb-3 border-warning/40 bg-warning-muted p-3 sm:p-4">
      <PendingUsageSummary
        totalEvents={pending.data.total_events}
        host={host}
        provider={provider}
        search={search}
        matchingEvents={pending.data.matching_events}
        hasDefaultKeyedOption={hasDefaultKeyedOption}
        selectedGroupCount={selectedGroups.length}
        selectedEventCount={selectedEventCount}
        onAssignSelected={openBatch}
        onClearSelection={() => setSelected({})}
      />
      <PendingUsageFilters
        host={host}
        provider={provider}
        search={search}
        sidecars={pending.data.sidecars}
        providers={pending.data.providers}
        machineNames={machineNames}
        totalGroups={pending.data.total_groups}
        selectingAll={selectingAll}
        onHostChange={(value) => updateFilter(setHost)(value)}
        onProviderChange={(value) => updateFilter(setProvider)(value)}
        onSearchChange={(value) => updateFilter(setSearch)(value)}
        onSelectAll={() => void selectAllMatching()}
      />
      <PendingUsageSessionsTable
        items={pending.data.items}
        machineNames={machineNames}
        accounts={accounts}
        selected={selected}
        assignPending={assign.isPending}
        batchPending={batch.isPending}
        optionGroupsForProvider={optionGroupsForProvider}
        validChoice={validChoice}
        findOption={findOption}
        optionNote={optionNote}
        providerName={providerName}
        onToggleSelected={(key, group, checked) => setSelected((current) => {
          const next = { ...current };
          if (checked) next[key] = group;
          else delete next[key];
          return next;
        })}
        onChooseAccount={(key, value) => setAccounts((old) => ({ ...old, [key]: value }))}
        onSetup={openWizard}
        onAssign={(group, chosen) => assign.mutate({
          eventIds: group.event_ids,
          accountId: decodeAssignTarget(chosen).accountId,
          targetProviderId: targetProviderFor(group.provider_id, chosen),
          sidecarId: group.sidecar_id,
        })}
      />
      <PendingUsagePagination
        offset={offset}
        itemCount={pending.data.items.length}
        totalGroups={pending.data.total_groups}
        matchingEvents={pending.data.matching_events}
        onPrevious={() => setOffset((page) => Math.max(0, page - 100))}
        onNext={() => setOffset((page) => page + 100)}
      />
      <ResponsiveDialog
        open={batchOpen && !wizardTarget}
        onOpenChange={setBatchOpen}
        title="Assign selected usage"
        description={`${selectedGroups.length} groups · ${selectedEventCount} events. Choose an account for each provider.`}
        width="max-w-xl"
      >
        <PendingUsageBatchAssign
          selectedProviders={selectedProviders}
          selectedGroups={selectedGroups}
          batchAccounts={batchAccounts}
          batchPending={batch.isPending}
          selectedEventCount={selectedEventCount}
          optionGroupsForProvider={optionGroupsForProvider}
          validChoice={validChoice}
          findOption={findOption}
          optionNote={optionNote}
          providerName={providerName}
          onSetup={openWizard}
          onChooseAccount={(providerId, value) => setBatchAccounts((old) => ({ ...old, [providerId]: value }))}
          onCancel={() => setBatchOpen(false)}
          onApply={applyBatch}
        />
      </ResponsiveDialog>
      {wizardTarget ? (
        <AddProviderWizard
          preScopedProvider={wizardTarget.provider}
          providers={configs.data?.providers ?? []}
          existingAccountIdsByProvider={existingAccountIdsByProvider}
          onClose={() => setWizardTarget(null)}
          onSaved={(savedProviderId, savedAccountId) => {
            const savedValue = encodeAssignTarget(
              savedProviderId || accountConfigProviderIdForUsage(wizardTarget.providerId),
              savedAccountId,
            );
            setBatchAccounts((old) => ({ ...old, [wizardTarget.providerId]: savedValue }));
            if (wizardTarget.rowKey) {
              setAccounts((old) => ({ ...old, [wizardTarget.rowKey!]: savedValue }));
            }
            setAccounts((old) => {
              const next = { ...old };
              for (const item of pending.data?.items ?? []) {
                if (item.provider_id === wizardTarget.providerId) {
                  next[sessionKey(item)] = savedValue;
                }
              }
              return next;
            });
            queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
            setWizardTarget(null);
          }}
        />
      ) : null}
    </Card>
  );
}
