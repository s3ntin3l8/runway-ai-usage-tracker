// Assignment rules: "this credential origin → that account". A rule only matters when a
// machine can't identify a credential on its own; removing one makes the credential
// reappear under "Needs mapping" if it still has no local identity.

import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { ListChecks, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { deleteCredentialTag, fetchCredentialTags } from '@/api/endpoints';
import type { CredentialTag } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { maskAccountId } from '@/lib/accountDisplay';

function ruleKey(t: CredentialTag): string {
  return `${t.provider_id}/${t.credential_origin}/${t.sidecar_id ?? '*'}`;
}

export function RulesView() {
  const invalidate = useInvalidateCredentialViews();
  const rules = useQuery({ queryKey: ['fleet', 'credential_tags'], queryFn: fetchCredentialTags });
  const sidecars = useSidecars();
  const names = buildSidecarNameMap(sidecars.data?.sidecars ?? []);
  const [removing, setRemoving] = useState<CredentialTag | null>(null);

  const remove = useMutation({
    mutationFn: (t: CredentialTag) => deleteCredentialTag(t),
    onSuccess: () => {
      toast.success('Rule removed');
      setRemoving(null);
      invalidate();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const items = rules.data?.items ?? [];

  return (
    <Card className="p-4">
      <h3 className="text-sm font-semibold">Assignment rules</h3>
      <p className="text-[11px] text-fg-subtle">
        When a machine can't tell which account a credential belongs to, a rule files it under
        the account you chose. A rule applies to one machine or to all of them.
      </p>
      {rules.isPending ? (
        <Skeleton className="mt-3 h-12 w-full" />
      ) : rules.isError ? (
        <p className="mt-3 text-[12px] text-critical">Couldn't load rules: {rules.error.message}</p>
      ) : items.length === 0 ? (
        <EmptyState
          icon={ListChecks}
          title="No assignment rules"
          description="Rules appear here once you assign a credential to an account."
        />
      ) : (
        <ul className="mt-2 divide-y divide-edge" aria-label="Assignment rules">
          {items.map((t) => (
            <li key={ruleKey(t)} className="flex items-center justify-between gap-3 py-2.5">
              <div className="min-w-0">
                <p className="truncate text-[13px]">
                  <span className="font-medium">{t.provider_id}</span>{' '}
                  <span className="font-mono text-[11px] text-fg-muted">{t.credential_origin}</span>
                  {' → '}
                  <span>{maskAccountId(t.account_id)}</span>
                  {t.target_provider_id && t.target_provider_id !== t.provider_id && (
                    <span className="text-fg-muted"> ({t.target_provider_id})</span>
                  )}
                </p>
                <p className="truncate text-[11px] text-fg-subtle">
                  {t.sidecar_id ? `on ${names.get(t.sidecar_id) ?? t.sidecar_id}` : 'on all machines'}
                </p>
              </div>
              <Button
                variant="danger-ghost"
                size="icon-sm"
                aria-label={`Remove rule ${ruleKey(t)}`}
                onClick={() => setRemoving(t)}
              >
                <Trash2 className="size-3.5" aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title="Remove this rule?"
        description={'The credential goes back to "Needs mapping" if its machine still can\'t identify it.'}
        pending={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing)}
      />
    </Card>
  );
}
