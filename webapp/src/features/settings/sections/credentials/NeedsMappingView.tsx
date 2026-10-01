// Credentials a machine reported that no account is known for, plus collected usage that
// couldn't be filed under an account. Both end the same way: pick the account.

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { CheckCircle2 } from 'lucide-react';
import { fetchUntaggedCredentials } from '@/api/endpoints';
import type { UntaggedCredential } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { PendingUsageEventsCard } from '@/features/fleet/PendingUsageEventsCard';
import { UntaggedCredentialsDialog } from '@/features/fleet/UntaggedCredentialsDialog';
import { useSidecars, buildSidecarNameMap } from '@/features/fleet/queries';
import { timeAgo } from '@/lib/format';

export function NeedsMappingView({ pendingUsageEvents }: { pendingUsageEvents: number }) {
  const untagged = useQuery({
    queryKey: ['fleet', 'untagged_credentials', 'all'],
    queryFn: () => fetchUntaggedCredentials(),
  });
  const sidecars = useSidecars();
  const names = buildSidecarNameMap(sidecars.data?.sidecars ?? []);
  const [resolving, setResolving] = useState<UntaggedCredential | 'all' | null>(null);

  const items = untagged.data?.items ?? [];

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold">Credentials without an account</h3>
            <p className="text-[11px] text-fg-subtle">
              A machine found these but couldn't tell which account they belong to. Pick the
              account and future data is filed under it.
            </p>
          </div>
          {items.length > 1 ? (
            <Button size="sm" onClick={() => setResolving('all')}>
              Assign all ({items.length})
            </Button>
          ) : null}
        </div>
        {untagged.isPending ? (
          <Skeleton className="mt-3 h-12 w-full" />
        ) : items.length === 0 ? (
          <EmptyState
            icon={CheckCircle2}
            title="Every credential has an account"
            description="Nothing is waiting to be mapped."
          />
        ) : (
          <ul className="mt-2 divide-y divide-edge" aria-label="Credentials without an account">
            {items.map((item) => (
              <li
                key={`${item.sidecar_id}/${item.provider_id}/${item.credential_origin}`}
                className="flex flex-wrap items-center justify-between gap-2 py-2.5"
              >
                <div className="min-w-0">
                  <p className="text-[13px] font-medium">
                    {item.provider_id}{' '}
                    <span className="font-mono text-[11px] font-normal text-fg-muted">
                      {item.credential_origin}
                    </span>
                  </p>
                  <p className="text-[11px] text-fg-subtle">
                    on {names.get(item.sidecar_id) ?? item.sidecar_id} · first seen{' '}
                    {timeAgo(item.first_seen)}
                    {item.claimed_account_id ? ` · claims ${item.claimed_account_id}` : ''}
                  </p>
                </div>
                <Button size="sm" variant="secondary" onClick={() => setResolving(item)}>
                  Assign account
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {pendingUsageEvents > 0 ? <PendingUsageEventsCard /> : null}

      <UntaggedCredentialsDialog
        open={resolving !== null}
        onClose={() => setResolving(null)}
        singleEntry={resolving && resolving !== 'all' ? resolving : undefined}
      />
    </div>
  );
}
