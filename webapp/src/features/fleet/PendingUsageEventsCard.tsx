import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  assignPendingUsageEvents,
  fetchPendingUsageSessions,
  fetchProviderConfigs,
} from '@/api/endpoints';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import type { PendingUsageSession } from '@/api/types';

function sessionKey(group: PendingUsageSession) {
  const sessionIdentity = group.session_id ? `session:${group.session_id}` : `event:${group.event_ids[0]}`;
  return JSON.stringify([group.provider_id, group.sidecar_id, sessionIdentity]);
}

function accountProviderIdForUsage(providerId: string) {
  return providerId === 'opencode-free' || providerId === 'opencode-zen' ? 'opencode' : providerId;
}

export function PendingUsageEventsCard() {
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [accounts, setAccounts] = useState<Record<string, string>>({});
  const pending = useQuery({
    queryKey: ['fleet', 'pending_usage_sessions', offset],
    queryFn: () => fetchPendingUsageSessions(offset),
    refetchInterval: 60_000,
  });
  const configs = useQuery({
    queryKey: ['system', 'provider-configs'],
    queryFn: fetchProviderConfigs,
  });
  const assign = useMutation({
    mutationFn: ({ eventIds, accountId }: { eventIds: number[]; accountId: string }) =>
      assignPendingUsageEvents(eventIds, accountId),
    onSuccess: ({ assigned }) => {
      toast.success(`${assigned} usage event${assigned === 1 ? '' : 's'} assigned`);
      setOffset(0);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_sessions'] });
      queryClient.invalidateQueries({ queryKey: ['fleet', 'pending_usage_events'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
    onError: (error) => toast.error(error.message),
  });
  if (pending.isPending) return null;
  if (pending.isError) {
    return (
      <Card className="mb-3 border-warning/40 bg-warning-muted p-3">
        <h2 className="text-sm font-semibold">Unassigned usage</h2>
        <p className="mt-1 text-xs text-fg-muted">Could not load unassigned usage events. Try refreshing the page.</p>
      </Card>
    );
  }
  if (!pending.data?.total_events) return null;

  const hasDefaultKeyedOption = (configs.data?.providers ?? []).some((provider) =>
    provider.accounts.some(
      (account) =>
        account.account_id === 'default' && !account.archived && account.enabled !== false,
    ),
  );

  return (
    <Card id="pending-events" className="mb-3 border-warning/40 bg-warning-muted p-3">
      <h2 className="text-sm font-semibold">Unassigned usage · {pending.data.total_events} events</h2>
      <p className="mt-1 text-xs text-fg-muted">
        These events are stored safely and excluded from account totals until assigned.
      </p>
      <p className="mt-1 text-xs text-fg-muted">
        Assigning also maps future default-identity events from that provider on this machine to the selected account.
      </p>
      {hasDefaultKeyedOption && (
        <p className="mt-1 text-xs text-fg-muted">
          An account labeled “(default)” below is still stored under the shared default identity, not its own
          account — assigning there keeps it shared. Re-key its config in Fleet first if you want it on its own
          account instead.
        </p>
      )}
      <div className="mt-3 flex max-h-[32rem] flex-col gap-2 overflow-y-auto">
        {pending.data.items.map((group) => {
          const key = sessionKey(group);
          const accountProviderId = accountProviderIdForUsage(group.provider_id);
          const configured = configs.data?.providers.find((provider) => provider.provider_id === accountProviderId);
          const options =
            configured?.accounts.filter((account) => !account.archived && account.enabled !== false) ?? [];
          const sessionLabel = group.session_id ?? `event ${group.event_ids[0]}`;
          const modelLabel = group.model_ids.length ? group.model_ids.join(', ') : 'unknown model';
          return (
            <div key={key} className="flex flex-wrap items-center gap-2 rounded border border-border p-2">
              <span className="min-w-24 text-xs font-medium">{group.provider_id}</span>
              <span className="max-w-72 truncate text-xs text-fg-muted" title={group.session_id ?? undefined}>
                {group.event_count} events · {new Date(group.first_ts).toLocaleString()}–
                {new Date(group.last_ts).toLocaleString()} · {modelLabel} · {group.sidecar_id}
              </span>
              <span className="max-w-48 truncate text-xs text-fg-muted" title={group.session_id ?? undefined}>
                {group.session_id ? `Session ${group.session_id}` : `No session ID · event ${group.event_ids[0]}`}
              </span>
              <select
                aria-label={`Account for ${group.provider_id} session ${sessionLabel}`}
                className="h-8 rounded border border-border bg-surface-1 px-2 text-xs"
                value={accounts[key] ?? ''}
                onChange={(change) => setAccounts((old) => ({ ...old, [key]: change.target.value }))}
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
              <Button
                size="sm"
                variant="primary"
                disabled={!accounts[key] || assign.isPending}
                loading={assign.isPending}
                onClick={() => assign.mutate({ eventIds: group.event_ids, accountId: accounts[key] })}
              >
                {group.session_id ? 'Assign session' : 'Assign event'}
              </Button>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex items-center justify-between text-xs text-fg-muted">
        <span>
          Showing {offset + 1}–{Math.min(offset + pending.data.items.length, pending.data.total_groups)} of{' '}
          {pending.data.total_groups} groups · {pending.data.total_events} events
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
    </Card>
  );
}
