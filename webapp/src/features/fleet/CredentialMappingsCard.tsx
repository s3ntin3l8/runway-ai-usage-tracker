// Resolved credential tags (silent-listener, PR #288 / #319).
//
// Once an origin is tagged it never shows up in the Untagged dialog again,
// so this card is where the operator sees — and removes — existing
// `origin → account_id` mappings. Removing a tag makes the sidecar drop
// the hint on its next /fleet/config refresh; if the origin still has no
// local identity it re-appears as untagged, ready to be re-mapped.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Trash2 } from 'lucide-react';

import { deleteCredentialTag, fetchCredentialTags } from '@/api/endpoints';
import type { CredentialTag } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { maskAccountId } from '@/lib/accountDisplay';

function tagKey(t: CredentialTag): string {
  return `${t.provider_id}/${t.credential_origin}/${t.sidecar_id ?? '*'}`;
}

export function CredentialMappingsCard({ className }: { className?: string }) {
  const queryClient = useQueryClient();
  const tags = useQuery({
    queryKey: ['fleet', 'credential_tags'],
    queryFn: fetchCredentialTags,
  });

  const remove = useMutation({
    mutationFn: (t: CredentialTag) => deleteCredentialTag(t),
    onSuccess: () => {
      toast.success('Mapping removed');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'credential_tags'] });
      queryClient.invalidateQueries({ queryKey: ['fleet', 'untagged_credentials'] });
    },
    onError: (err: Error) => {
      toast.error(err.message);
    },
  });

  const items = tags.data?.items ?? [];
  if (items.length === 0) return null;

  return (
    <Card className={`p-3 ${className ?? ''}`}>
      <p className="text-[13px] font-semibold">Credential mappings</p>
      <p className="text-[11px] text-fg-subtle">
        Operator-assigned accounts for sidecar credentials that carry no identity of their own.
      </p>
      <ul className="mt-2 divide-y divide-border" aria-label="Credential mappings">
        {items.map((t) => (
          <li key={tagKey(t)} className="flex items-center justify-between gap-3 py-2">
            <div className="min-w-0">
              <p className="truncate text-[12px]">
                <span className="font-medium">{t.provider_id}</span>{' '}
                <span className="font-mono text-[11px] text-fg-muted">{t.credential_origin}</span>
                {' → '}
                <span className="font-mono">{maskAccountId(t.account_id)}</span>
              </p>
              <p className="truncate text-[11px] text-fg-subtle">
                {t.sidecar_id ? (
                  <>
                    on <span className="font-mono">{t.sidecar_id}</span>
                  </>
                ) : (
                  'all machines'
                )}
              </p>
            </div>
            <Button
              size="sm"
              aria-label={`Remove mapping ${tagKey(t)}`}
              onClick={() => remove.mutate(t)}
              loading={Boolean(remove.isPending && remove.variables && tagKey(remove.variables) === tagKey(t))}
            >
              <Trash2 className="size-3.5" aria-hidden />
              Remove
            </Button>
          </li>
        ))}
      </ul>
    </Card>
  );
}
