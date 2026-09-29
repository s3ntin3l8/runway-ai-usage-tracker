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
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import type { PendingUsageAssignmentGroup, PendingUsageFilter, PendingUsageSession } from '@/api/types';
import { accountConfigProviderIdForUsage } from '@/lib/opencodeTiers';

function sessionKey(group: PendingUsageSession) {
  const sessionIdentity = group.session_id ? `session:${group.session_id}` : `event:${group.event_ids[0]}`;
  return JSON.stringify([group.provider_id, group.sidecar_id, sessionIdentity]);
}

function groupLabel(group: PendingUsageSession) {
  return group.session_id ?? `event ${group.event_ids[0]}`;
}

export function PendingUsageEventsCard() {
  const queryClient = useQueryClient();
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
    mutationFn: ({ eventIds, accountId }: { eventIds: number[]; accountId: string; sidecarId: string }) =>
      assignPendingUsageEvents(eventIds, accountId),
    onSuccess: ({ assigned, provider_id }, variables) => {
      toast.success(
        `${assigned} usage event${assigned === 1 ? '' : 's'} assigned. Future ${provider_id} events from ${variables.sidecarId} will use the selected account after its next config sync.`,
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
    {
      assigned: number;
      providers: string[];
      mappings: { provider_id: string; sidecar_id: string; account_id: string }[];
    },
    Error,
    PendingUsageAssignmentGroup[]
  >({
    mutationFn: (assignments) => assignPendingUsageEventsBatch(assignments),
    onSuccess: ({ assigned, mappings }) => {
      toast.success(
        `${assigned} usage event${assigned === 1 ? '' : 's'} assigned. Future attribution updated for ${mappings.length} provider/host pair${mappings.length === 1 ? '' : 's'} after the next config sync.`,
      );
      setSelected({});
      setBatchAccounts({});
      setBatchOpen(false);
      setOffset(0);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_sessions'] });
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_events'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
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
  const optionsForProvider = (providerId: string) => {
    const configProviderId = accountConfigProviderIdForUsage(providerId);
    const configured = configs.data?.providers.find((item) => item.provider_id === configProviderId);
    return configured?.accounts.filter((account) => !account.archived && account.enabled !== false) ?? [];
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
      toast.error(error instanceof Error ? error.message : 'Could not select matching usage groups');
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
    const assignments = [...byProvider.entries()].map(([providerId, eventIds]) => ({
      event_ids: eventIds,
      account_id: batchAccounts[providerId] ?? '',
    }));
    if (assignments.some((assignment) => !assignment.account_id)) return;
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
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Unassigned usage · {pending.data.total_events} events</h2>
          {(host || provider || search) && (
            <p className="mt-1 text-xs text-fg-muted">
              {pending.data.matching_events} events match the current filters.
            </p>
          )}
          <p className="mt-1 max-w-4xl text-xs text-fg-muted">
            These events are stored safely and excluded from account totals until assigned. Assigning maps future
            default-identity events for the same provider on the same host after its next config sync.
          </p>
          {hasDefaultKeyedOption && (
            <p className="mt-1 max-w-4xl text-xs text-fg-muted">
              An account labeled “(default)” remains under the shared default identity. Re-key its config in Fleet
              first to assign usage to its own account.
            </p>
          )}
        </div>
        {selectedGroups.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-fg-muted">
              {selectedGroups.length} groups · {selectedEventCount} events selected
            </span>
            <Button size="sm" variant="secondary" onClick={openBatch}>
              Assign selected
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelected({})}>
              Clear selection
            </Button>
          </div>
        )}
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-[minmax(12rem,1fr)_minmax(12rem,1fr)_minmax(16rem,2fr)_auto]">
        <label className="text-xs font-medium text-fg-muted">
          Host
          <select
            aria-label="Filter unassigned usage by host"
            className="mt-1 h-9 w-full rounded-sm border border-edge bg-surface-2 px-2 text-[13px] text-fg"
            value={host}
            onChange={(event) => updateFilter(setHost)(event.target.value)}
          >
            <option value="">All hosts</option>
            {pending.data.sidecars.map((sidecar) => <option key={sidecar} value={sidecar}>{sidecar}</option>)}
          </select>
        </label>
        <label className="text-xs font-medium text-fg-muted">
          Provider
          <select
            aria-label="Filter unassigned usage by provider"
            className="mt-1 h-9 w-full rounded-sm border border-edge bg-surface-2 px-2 text-[13px] text-fg"
            value={provider}
            onChange={(event) => updateFilter(setProvider)(event.target.value)}
          >
            <option value="">All providers</option>
            {pending.data.providers.map((providerId) => <option key={providerId} value={providerId}>{providerId}</option>)}
          </select>
        </label>
        <label className="text-xs font-medium text-fg-muted">
          Search session or model
          <Input
            className="mt-1 h-9"
            aria-label="Search unassigned sessions and models"
            placeholder="Search…"
            value={search}
            onChange={(event) => updateFilter(setSearch)(event.target.value)}
          />
        </label>
        {host && pending.data.total_groups > 0 && (
          <Button
            className="self-end"
            size="sm"
            variant="secondary"
            loading={selectingAll}
            disabled={selectingAll}
            onClick={() => void selectAllMatching()}
          >
            Select all {pending.data.total_groups} matching groups
          </Button>
        )}
      </div>

      <div className="mt-3 overflow-hidden rounded border border-border bg-surface-1">
        <Table className="min-w-[900px] text-xs">
          <THead className="bg-surface-2">
            <TR>
              <TH className="w-10"><span className="sr-only">Select</span></TH>
              <TH>Provider</TH>
              <TH>Host</TH>
              <TH>Session</TH>
              <TH>Events · time range</TH>
              <TH>Account</TH>
              <TH><span className="sr-only">Action</span></TH>
            </TR>
          </THead>
          <TBody>
            {pending.data.items.map((group) => {
              const key = sessionKey(group);
              const selectedHere = Boolean(selected[key]);
              const options = optionsForProvider(group.provider_id);
              const models = group.model_ids.length ? group.model_ids.join(', ') : 'Unknown model';
              const first = new Date(group.first_ts).toLocaleString();
              const last = new Date(group.last_ts).toLocaleString();
              return (
                <TR key={key} className={selectedHere ? 'bg-accent-muted/40' : undefined}>
                  <TD>
                    <input
                      type="checkbox"
                      aria-label={`Select ${group.provider_id} on ${group.sidecar_id} session ${groupLabel(group)}`}
                      checked={selectedHere}
                      onChange={(event) => setSelected((current) => {
                        const next = { ...current };
                        if (event.target.checked) next[key] = group;
                        else delete next[key];
                        return next;
                      })}
                    />
                  </TD>
                  <TD className="font-medium">{group.provider_id}</TD>
                  <TD className="max-w-36 truncate" title={group.sidecar_id}>{group.sidecar_id}</TD>
                  <TD className="max-w-64">
                    <details className="group">
                      <summary className="cursor-pointer truncate font-medium" title={groupLabel(group)}>
                        {group.session_id ? `Session ${group.session_id}` : `No session ID · event ${group.event_ids[0]}`}
                      </summary>
                      <p className="mt-1 break-all text-[11px] text-fg-muted">Models: {models}</p>
                    </details>
                  </TD>
                  <TD className="whitespace-nowrap text-fg-muted">
                    <span className="font-medium text-fg">{group.event_count}</span> · {first}
                    {first !== last ? ` – ${last}` : ''}
                  </TD>
                  <TD>
                    <select
                      aria-label={`Account for ${group.provider_id} session ${groupLabel(group)}`}
                      className="h-8 min-w-40 max-w-52 rounded-sm border border-edge bg-surface-2 px-2 text-xs"
                      value={accounts[key] ?? ''}
                      onChange={(event) => setAccounts((old) => ({ ...old, [key]: event.target.value }))}
                    >
                      <option value="">Choose account…</option>
                      {options.map((account) => {
                        const label = account.account_label || account.account_id;
                        return (
                          <option key={account.account_id} value={account.account_id}>
                            {account.account_id === 'default' ? `${label} (default)` : label}
                          </option>
                        );
                      })}
                    </select>
                  </TD>
                  <TD>
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={!accounts[key] || assign.isPending || batch.isPending}
                      loading={assign.isPending}
                      onClick={() => assign.mutate({
                        eventIds: group.event_ids,
                        accountId: accounts[key],
                        sidecarId: group.sidecar_id,
                      })}
                    >
                      {group.session_id ? 'Assign' : 'Assign event'}
                    </Button>
                  </TD>
                </TR>
              );
            })}
            {pending.data.items.length === 0 && (
              <TR><TD colSpan={7} className="py-8 text-center text-fg-muted">No unassigned usage matches these filters.</TD></TR>
            )}
          </TBody>
        </Table>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-fg-muted">
        <span>
          {pending.data.total_groups === 0
            ? 'No matching groups'
            : `Showing ${offset + 1}–${Math.min(offset + pending.data.items.length, pending.data.total_groups)} of ${pending.data.total_groups} groups · ${pending.data.matching_events} matching events`}
        </span>
        <div className="flex gap-2">
          <Button size="sm" disabled={offset === 0} onClick={() => setOffset((page) => Math.max(0, page - 100))}>
            Previous
          </Button>
          <Button
            size="sm"
            disabled={offset + 100 >= pending.data.total_groups}
            onClick={() => setOffset((page) => page + 100)}
          >
            Next
          </Button>
        </div>
      </div>

      <ResponsiveDialog
        open={batchOpen}
        onOpenChange={setBatchOpen}
        title="Assign selected usage"
        description={`${selectedGroups.length} groups · ${selectedEventCount} events. Choose an account for each provider.`}
        width="max-w-xl"
      >
        <div className="space-y-3">
          {selectedProviders.map((providerId) => {
            const groups = selectedGroups.filter((group) => group.provider_id === providerId);
            const count = groups.reduce((sum, group) => sum + group.event_count, 0);
            return (
              <label key={providerId} className="grid gap-1 text-xs font-medium text-fg-muted sm:grid-cols-[minmax(9rem,1fr)_2fr] sm:items-center">
                <span>{providerId} · {count} events</span>
                <select
                  aria-label={`Batch account for ${providerId}`}
                  className="h-9 w-full rounded-sm border border-edge bg-surface-2 px-2 text-[13px] text-fg"
                  value={batchAccounts[providerId] ?? ''}
                  onChange={(event) => setBatchAccounts((old) => ({ ...old, [providerId]: event.target.value }))}
                >
                  <option value="">Choose account…</option>
                  {optionsForProvider(providerId).map((account) => {
                    const label = account.account_label || account.account_id;
                    return (
                      <option key={account.account_id} value={account.account_id}>
                        {account.account_id === 'default' ? `${label} (default)` : label}
                      </option>
                    );
                  })}
                </select>
              </label>
            );
          })}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setBatchOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              disabled={!selectedProviders.length || selectedProviders.some((providerId) => !batchAccounts[providerId]) || batch.isPending}
              loading={batch.isPending}
              onClick={applyBatch}
            >
              Assign {selectedEventCount} events
            </Button>
          </div>
        </div>
      </ResponsiveDialog>
    </Card>
  );
}
