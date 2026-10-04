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
  originTitle,
  relativeExpiry,
  tokenSummary,
} from './display';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';

interface SourceRowProps {
  source: CredentialSourceView;
  /** Show which provider/account the row belongs to (the By-machine view). */
  context?: string;
  /** Show the Machine column; off when the list is already scoped to one machine. */
  showMachine?: boolean;
}

// Column templates live here as literals so Tailwind sees them; SourceList's header reuses them.
const GRID_WITH_MACHINE =
  'md:grid-cols-[6.5rem_minmax(0,1fr)_9.5rem_8.5rem_6rem_3.75rem]';
const GRID_WITHOUT_MACHINE = 'md:grid-cols-[6.5rem_minmax(0,1fr)_8.5rem_6rem_3.75rem]';

export const sourceGrid = (showMachine: boolean) =>
  showMachine ? GRID_WITH_MACHINE : GRID_WITHOUT_MACHINE;

export function SourceRow({ source, context, showMachine = true }: SourceRowProps) {
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

  const dead =
    source.status === 'expired' || source.status === 'invalid' || source.status === 'failing';
  // An offline machine can't be fixed by signing in again on it; it's probably retired.
  const fixHint = !dead
    ? null
    : source.machine_stale
      ? 'Machine offline — remove this credential if the machine is retired.'
      : source.login_hint
        ? `To fix: ${source.login_hint} on ${where}.`
        : null;

  const title = originTitle(source);
  const detail = [
    source.origin_path,
    source.token_types.length > 0 ? source.token_types.join(', ') : null,
  ].filter(Boolean);

  return (
    <li
      className={`flex flex-wrap items-start gap-x-3 gap-y-1 py-2 md:grid md:items-center ${sourceGrid(showMachine)}`}
    >
      <div className="flex flex-wrap items-center gap-1">
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
        {!source.enabled ? <Badge variant="neutral">Disabled</Badge> : null}
      </div>
      <div className="min-w-0">
        <Tooltip
          content={
            <div className="space-y-0.5">
              <p className="font-medium">{title}</p>
              {detail.map((d) => (
                <p key={d} className="break-all font-mono text-[11px] text-fg-muted">
                  {d}
                </p>
              ))}
              <p>
                {MAPPING_LABEL[source.mapping]}
                {source.mapping_scope === 'all_machines' ? ' (all machines)' : ''}
                {' — '}
                {MAPPING_HINT[source.mapping]}
              </p>
            </div>
          }
        >
          <p tabIndex={0} className="truncate text-[13px] font-medium">
            {title}
          </p>
        </Tooltip>
        <p className="truncate text-[11px] text-fg-subtle">
          {[context, tokenSummary(source.token_types), MAPPING_LABEL[source.mapping]]
            .filter(Boolean)
            .join(' · ')}
          {source.mapping_scope === 'all_machines' ? ' (all machines)' : ''}
        </p>
      </div>
      {showMachine ? (
        <div className="flex min-w-0 items-center gap-1 text-[12px] text-fg-muted">
          <span className="truncate">{where}</span>
          {source.machine_stale ? (
            <Tooltip content={`This machine hasn't checked in; last report ${timeAgo(source.last_seen)}.`}>
              <Badge variant="warning">offline</Badge>
            </Tooltip>
          ) : null}
        </div>
      ) : null}
      <div className="text-[12px] text-fg-muted">
        <p>
          <span className="sr-only">Expires: </span>
          {source.status === 'stale'
            ? `last reported ${timeAgo(source.last_seen)}`
            : relativeExpiry(source.expires_in_seconds)}
        </p>
        {source.refreshed_by === 'machine' ? (
          <p
            className="text-[11px] text-fg-subtle"
            title="This login belongs to the machine's CLI, which renews it. Refreshing it from here would sign that CLI out."
          >
            renewed by its machine
          </p>
        ) : source.rollable ? (
          <p className="text-[11px] text-fg-subtle">auto-refreshed</p>
        ) : null}
        {source.keep_alive ? (
          <p
            className="text-[11px] text-fg-subtle"
            title={
              source.keep_alive === 'on'
                ? 'The sidecar on this machine renews this login itself (--keep-alive).'
                : 'Turn keep-alive on for this machine in Fleet (or start its sidecar with --keep-alive) so this login is renewed even when the CLI is idle.' +
                  (source.keep_alive === 'unknown' ? ' This sidecar is too old to report it.' : '')
            }
          >
            keep-alive: {source.keep_alive}
          </p>
        ) : null}
      </div>
      <div className="text-[12px] text-fg-muted">
        <span className="sr-only">Last collected: </span>
        {source.last_success_at ? (
          timeAgo(source.last_success_at)
        ) : source.health === 'untried' ? (
          <span title="This credential is registered but has never been used for a collection.">
            not yet tried
          </span>
        ) : (
          'never'
        )}
      </div>
      <div className="ml-auto flex shrink-0 items-center justify-end gap-1 md:ml-0">
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
      {(source.mapping === 'operator' &&
        source.mapping_scope === 'all_machines' &&
        !source.fingerprinted) ||
      source.unused_reason ||
      source.last_error ||
      fixHint ? (
        <div className="w-full space-y-0.5 md:col-span-full">
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
          {fixHint ? <p className="text-[11px] text-fg-muted">{fixHint}</p> : null}
        </div>
      ) : null}
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
