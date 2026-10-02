// One credential source: where it was found, why it maps to its account, whether it is
// healthy, and — for the source behind the account's data — that it is the active one.

import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { RefreshCw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { deleteCredentialSource, postCredentialSourceRefresh } from '@/api/endpoints';
import type { CredentialSourceView } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { Tooltip } from '@/components/ui/Tooltip';
import { timeAgo } from '@/lib/format';
import {
  MAPPING_HINT,
  MAPPING_LABEL,
  STATUS_HINT,
  STATUS_LABEL,
  STATUS_VARIANT,
  UNUSED_HINT,
  relativeExpiry,
} from './display';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';

interface SourceRowProps {
  source: CredentialSourceView;
  /** Show which provider/account the row belongs to (the By-machine view). */
  context?: string;
}

export function SourceRow({ source, context }: SourceRowProps) {
  const invalidate = useInvalidateCredentialViews();
  const [confirming, setConfirming] = useState(false);

  const refresh = useMutation({
    mutationFn: () =>
      postCredentialSourceRefresh(source.provider_id, source.account_id, source.source_id),
    onSuccess: () => {
      toast.success('Token refreshed');
      invalidate();
    },
    onError: (err: Error) => toast.error(`Refresh failed: ${err.message}`),
  });

  const remove = useMutation({
    mutationFn: () =>
      deleteCredentialSource(source.provider_id, source.account_id, source.source_id),
    onSuccess: () => {
      toast.success('Credential removed');
      setConfirming(false);
      invalidate();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const where =
    source.origin_kind === 'machine'
      ? (source.machine_name ?? source.machine_id ?? 'machine')
      : source.origin_kind === 'server'
        ? 'Server'
        : 'Settings';

  return (
    <li className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2 py-2.5">
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <Tooltip content={STATUS_HINT[source.status] ?? ''}>
            <Badge variant={STATUS_VARIANT[source.status] ?? 'neutral'}>
              {STATUS_LABEL[source.status] ?? source.status}
            </Badge>
          </Tooltip>
          {source.unused_reason ? (
            <Tooltip content={UNUSED_HINT[source.unused_reason]}>
              <Badge variant="warning">Not used</Badge>
            </Tooltip>
          ) : null}
          {source.is_active ? (
            <Tooltip content="This credential produced the account's most recent successful collection.">
              <Badge variant="accent">Active</Badge>
            </Tooltip>
          ) : null}
          <span className="text-[13px] font-medium">{source.label}</span>
          <span className="text-[12px] text-fg-muted">· {where}</span>
          {context ? <span className="text-[12px] text-fg-subtle">· {context}</span> : null}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-fg-subtle">
          <Tooltip content={MAPPING_HINT[source.mapping]}>
            <span>
              {MAPPING_LABEL[source.mapping]}
              {source.mapping_scope === 'all_machines' ? ' (all machines)' : ''}
            </span>
          </Tooltip>
          {source.token_types.length > 0 ? <span>{source.token_types.join(', ')}</span> : null}
          <span>
            {source.status === 'stale'
              ? `last reported ${timeAgo(source.last_seen)}`
              : relativeExpiry(source.expires_in_seconds)}
          </span>
          {source.last_success_at ? (
            <span>collected {timeAgo(source.last_success_at)}</span>
          ) : source.health === 'untried' ? (
            <span title="This credential is registered but has never been used for a collection.">
              not yet tried
            </span>
          ) : null}
          {source.refreshed_by === 'machine' ? (
            <span title="This login belongs to the machine's CLI, which renews it. Refreshing it from here would sign that CLI out.">
              renewed by its machine
            </span>
          ) : source.rollable ? (
            <span>auto-refreshed</span>
          ) : null}
          {!source.enabled ? <span>disabled</span> : null}
        </div>
        {source.mapping === 'operator' &&
        source.mapping_scope === 'all_machines' &&
        !source.fingerprinted ? (
          <p className="text-[11px] text-warning">
            This rule applies on every machine and isn't tied to the credential itself — if the
            account behind it changes, data keeps landing on the old one.
          </p>
        ) : null}
        {source.unused_reason ? (
          <p className="text-[11px] text-warning">{UNUSED_HINT[source.unused_reason]}</p>
        ) : null}
        {source.last_error ? (
          <p className="text-[11px] text-critical">Last attempt: {source.last_error}</p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {source.can_refresh ? (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`Refresh ${source.label}`}
            loading={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            <RefreshCw className="size-3.5" aria-hidden />
          </Button>
        ) : null}
        {source.removable ? (
          <Button
            variant="danger-ghost"
            size="icon-sm"
            aria-label={`Remove ${source.label}`}
            onClick={() => setConfirming(true)}
          >
            <Trash2 className="size-3.5" aria-hidden />
          </Button>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Remove ${source.label}?`}
        description={`Forgets this credential from ${where}. It comes back on the machine's next report if the credential is still there.`}
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </li>
  );
}
