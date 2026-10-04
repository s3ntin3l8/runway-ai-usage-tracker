// The credential rows under one account or machine: a column header, the rows that matter,
// and the dead ones folded away behind a toggle so a long-expired login doesn't push the
// healthy ones down the page.

import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { removeCredentialSources } from '@/api/endpoints';
import type { CredentialSourceView, SourceProbeResult } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { isInactive, originSummary } from './display';
import { SourceRow, sourceGrid } from './SourceRow';

interface SourceListProps {
  sources: CredentialSourceView[];
  label: string;
  /** Provider/account shown under each row's name (the By-machine view). */
  contextFor?: (source: CredentialSourceView) => string;
  /** The list is already scoped to one machine, so the Where column is redundant. */
  showMachine?: boolean;
  providerName?: string;
  /** Set when the list is one account's: enables "remove stale" (the endpoint is per account). */
  account?: { provider_id: string; account_id: string };
  /** Results of a live re-test, by source id; `probed` once one has run. */
  probes?: Map<string, SourceProbeResult>;
  probed?: boolean;
}

/** Gone for good: on a machine that stopped checking in, or no longer reported by it. */
const isStale = (s: CredentialSourceView) =>
  s.removable && isInactive(s) && (s.machine_stale === true || s.status === 'stale');

export function SourceList({
  sources,
  label,
  contextFor,
  showMachine = true,
  providerName,
  account,
  probes,
  probed = false,
}: SourceListProps) {
  const invalidate = useInvalidateCredentialViews();
  const [expanded, setExpanded] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const inactive = sources.filter(isInactive);
  const healthy = sources.filter((s) => !isInactive(s));
  const stale = account ? sources.filter(isStale) : [];
  // Only fold when something healthy remains; a list that is all dead shows all of it.
  const folded = healthy.length > 0 && inactive.length > 0 && !expanded;
  const visible = folded ? healthy : sources;
  const grid = sourceGrid(showMachine);

  const remove = useMutation({
    mutationFn: () =>
      removeCredentialSources(
        account!.provider_id,
        account!.account_id,
        stale.map((s) => s.source_id),
      ),
    onSuccess: (res) => {
      const skipped = res.skipped.length;
      toast.success(
        `Removed ${res.removed.length} ${res.removed.length === 1 ? 'credential' : 'credentials'}` +
          (skipped > 0 ? `; ${skipped} could not be removed` : ''),
      );
      setConfirming(false);
      invalidate();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div className="mt-1">
      <div
        aria-hidden
        className={`hidden gap-x-3 border-b border-edge pb-1 text-[10px] font-medium uppercase tracking-wide text-fg-subtle md:grid ${grid}`}
      >
        <span>Status</span>
        <span>Credential</span>
        {showMachine ? <span>Machine</span> : null}
        <span>Expires</span>
        <span>Last collected</span>
        <span />
      </div>
      <ul className="divide-y divide-edge" aria-label={label}>
        {visible.map((s) => (
          <SourceRow
            key={`${s.provider_id}/${s.account_id}/${s.source_id}`}
            source={s}
            context={contextFor?.(s)}
            showMachine={showMachine}
            providerName={providerName}
            probed={probed}
            probe={probes?.get(s.source_id)}
          />
        ))}
      </ul>
      <div className="mt-1 flex flex-wrap items-center gap-x-3">
        {healthy.length > 0 && inactive.length > 0 ? (
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            className="flex items-center gap-1 rounded-sm py-1 text-[11px] text-fg-muted hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {expanded ? (
              <ChevronDown className="size-3" aria-hidden />
            ) : (
              <ChevronRight className="size-3" aria-hidden />
            )}
            {expanded
              ? 'Hide inactive credentials'
              : `Show ${inactive.length} inactive ${inactive.length === 1 ? 'credential' : 'credentials'}`}
          </button>
        ) : null}
        {stale.length > 0 ? (
          <Button variant="danger-ghost" size="sm" onClick={() => setConfirming(true)}>
            <Trash2 className="size-3.5" aria-hidden />
            Remove {stale.length} stale
          </Button>
        ) : null}
      </div>
      <ResponsiveDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Remove ${stale.length} stale ${stale.length === 1 ? 'credential' : 'credentials'}?`}
        description="These are on machines that stopped checking in, or are no longer reported by their machine. They come back if the machine reports them again."
      >
        <ul className="max-h-60 space-y-1 overflow-auto text-[12px]">
          {stale.map((s) => (
            <li key={s.source_id}>{originSummary(s)}</li>
          ))}
        </ul>
        <div className="mt-4 flex justify-end gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setConfirming(false)}
            disabled={remove.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            size="sm"
            loading={remove.isPending}
            onClick={() => remove.mutate()}
          >
            Remove
          </Button>
        </div>
      </ResponsiveDialog>
    </div>
  );
}
