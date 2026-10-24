// Credential sources a machine found but could not read: a browser cookie store the
// browser holds open, or one whose values are app-bound encrypted. The provider is not
// "not signed in" — its credential exists, the sidecar just cannot read it — so these
// are listed here rather than among the credentials waiting for an account.

import { CheckCircle2, Lock } from 'lucide-react';
import type { BlockedSource } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { timeAgo } from '@/lib/format';

const REASON_LABEL: Record<string, string> = {
  source_locked: 'Locked',
  source_unsupported: 'Unsupported format',
  source_unreadable: 'Unreadable',
};

const reasonLabel = (reason: string): string => REASON_LABEL[reason] ?? reason;

export function UnavailableSourcesView({ sources }: { sources: BlockedSource[] }) {
  if (sources.length === 0) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title="Every credential source can be read"
        description="Nothing is blocked: each machine can open every store it needs."
      />
    );
  }

  return (
    <Card className="p-4">
      <div>
        <h3 className="text-sm font-semibold">Credential sources that could not be read</h3>
        <p className="text-[11px] text-fg-subtle">
          A machine found these but could not open them, so the provider's quota stays dark
          until the store is readable or the credential is supplied another way.
        </p>
      </div>
      <ul className="mt-2 divide-y divide-edge" aria-label="Credential sources that could not be read">
        {sources.map((source) => (
          <li
            key={`${source.sidecar_id}/${source.provider_id}/${source.credential_origin}`}
            className="flex flex-wrap items-start justify-between gap-2 py-2.5"
          >
            <div className="min-w-0">
              <p className="text-[13px] font-medium">
                {source.provider_id}{' '}
                <span className="font-mono text-[11px] font-normal text-fg-muted">
                  {source.source_store ?? source.credential_origin}
                </span>
              </p>
              <p className="text-[11px] text-fg-subtle">
                on {source.sidecar_id}
                {source.first_seen ? ` · first seen ${timeAgo(source.first_seen)}` : ''}
              </p>
              {source.detail ? (
                <p className="text-[11px] text-fg-muted">{source.detail}</p>
              ) : null}
              {source.remedy ? (
                <p className="text-[11px] text-fg-muted">Fix: {source.remedy}</p>
              ) : null}
              {source.collecting ? (
                <p className="text-[11px] text-fg-subtle">
                  This provider is still collecting from another source.
                </p>
              ) : null}
            </div>
            <Badge variant={source.collecting ? 'neutral' : 'critical'}>
              <Lock className="size-3" aria-hidden />
              {reasonLabel(source.reason)}
            </Badge>
          </li>
        ))}
      </ul>
    </Card>
  );
}
