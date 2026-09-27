import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { assignPendingUsageEvents, fetchPendingUsageEvents, fetchProviderConfigs } from '@/api/endpoints';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';

export function PendingUsageEventsCard() {
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);
  const pending = useQuery({
    queryKey: ['fleet', 'pending_usage_events', offset],
    queryFn: () => fetchPendingUsageEvents(offset),
    refetchInterval: 60_000,
  });
  const configs = useQuery({
    queryKey: ['system', 'provider-configs'],
    queryFn: fetchProviderConfigs,
  });
  const [accounts, setAccounts] = useState<Record<string, string>>({});
  const assign = useMutation({
    mutationFn: ({ id, accountId }: { id: number; accountId: string }) =>
      assignPendingUsageEvents([id], accountId),
    onSuccess: () => {
      toast.success('Usage events assigned');
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
  if (!pending.data?.total) return null;

  // Some providers still carry a config row keyed account_id="default" with
  // a real-looking label (e.g. an email) — the label is cosmetic, but
  // selecting that option here still assigns the event onto the shared
  // "default" bucket, not a per-account row. Surfaced explicitly below and
  // on the option itself so assigning to it is a deliberate choice, not a
  // trap. See docs/migration-v3.md — re-key the config in Fleet first if
  // that's not what's wanted.
  const hasDefaultKeyedOption = (configs.data?.providers ?? []).some((p) =>
    p.accounts.some((a) => a.account_id === 'default' && a.source !== 'discovered' && a.enabled !== false),
  );

  return (
    <Card id="pending-events" className="mb-3 border-warning/40 bg-warning-muted p-3">
      <h2 className="text-sm font-semibold">Unassigned usage · {pending.data.total} events</h2>
      <p className="mt-1 text-xs text-fg-muted">
        These events are stored safely and excluded from account totals until assigned.
      </p>
      <p className="mt-1 text-xs text-fg-muted">
        Assigning also maps future default-identity events from that provider on this machine to the selected account.
      </p>
      {hasDefaultKeyedOption && (
        <p className="mt-1 text-xs text-fg-muted">
          An account labeled "(default)" below is still stored under the shared default identity, not its own
          account — assigning there keeps it shared. Re-key its config in Fleet first if you want it on its own
          account instead.
        </p>
      )}
      <div className="mt-3 flex max-h-[32rem] flex-col gap-2 overflow-y-auto">
        {pending.data.items.map((event) => {
          const providerId = event.provider_id;
          const configured = configs.data?.providers.find((p) => p.provider_id === providerId);
          const options = configured?.accounts.filter((a) => a.source !== 'discovered' && a.enabled !== false) ?? [];
          return (
            <div key={event.id} className="flex flex-wrap items-center gap-2 rounded border border-border p-2">
              <span className="min-w-24 text-xs font-medium">{providerId}</span>
              <span className="max-w-64 truncate text-xs text-fg-muted" title={event.event_id}>
                {new Date(event.ts).toLocaleString()} · {event.model_id || 'unknown model'} · {event.sidecar_id} · {event.event_id}
              </span>
              <select
                aria-label={`Account for ${providerId} event ${event.event_id}`}
                className="h-8 rounded border border-border bg-surface-1 px-2 text-xs"
                value={accounts[String(event.id)] ?? ''}
                onChange={(change) => setAccounts((old) => ({ ...old, [String(event.id)]: change.target.value }))}
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
                disabled={!accounts[String(event.id)] || assign.isPending}
                loading={assign.isPending}
                onClick={() => assign.mutate({ id: event.id, accountId: accounts[String(event.id)] })}
              >Assign event</Button>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex items-center justify-between text-xs text-fg-muted">
        <span>
          Showing {offset + 1}–{Math.min(offset + pending.data.items.length, pending.data.total)} of {pending.data.total}
        </span>
        <div className="flex gap-2">
          <Button size="sm" disabled={offset === 0} onClick={() => setOffset((page) => Math.max(0, page - 100))}>
            Previous
          </Button>
          <Button size="sm" disabled={offset + 100 >= pending.data.total} onClick={() => setOffset((page) => page + 100)}>
            Next
          </Button>
        </div>
      </div>
    </Card>
  );
}
