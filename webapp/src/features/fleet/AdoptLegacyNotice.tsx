// Offers to fold a re-paired machine's old shared-key entry into its new enrolled card.
// Re-pairing makes a second sidecar for the same machine; this merges them back into one.
// Nothing happens without a click: a hostname is a label, not proof of identity.

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { adoptLegacySidecar } from '@/api/endpoints';
import type { AdoptableSidecar } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { formatNumber } from '@/lib/format';

const dismissKey = (sidecarId: string, legacyId: string) =>
  `runway.fleet.adopt-dismissed.${sidecarId}.${legacyId}`;

function isDismissed(sidecarId: string, legacyId: string): boolean {
  try {
    return window.localStorage.getItem(dismissKey(sidecarId, legacyId)) === '1';
  } catch {
    return false;
  }
}

const plural = (n: number, noun: string) => `${formatNumber(n)} ${noun}${n === 1 ? '' : 's'}`;

export function AdoptLegacyNotice({
  sidecarId,
  candidates,
}: {
  sidecarId: string;
  candidates: AdoptableSidecar[];
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [, setDismissedTick] = useState(0);
  const legacy = candidates.find((c) => !isDismissed(sidecarId, c.sidecar_id));

  // The preview is read only when the dialog opens, so an idle Fleet page costs nothing.
  const preview = useQuery({
    queryKey: ['fleet', 'adopt-preview', sidecarId, legacy?.sidecar_id],
    queryFn: () => adoptLegacySidecar(sidecarId, legacy!.sidecar_id, true),
    enabled: open && legacy !== undefined,
  });

  const adopt = useMutation({
    mutationFn: () => adoptLegacySidecar(sidecarId, legacy!.sidecar_id, false),
    onSuccess: () => {
      toast.success(`Adopted ${legacy?.sidecar_id}`);
      setOpen(false);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });

  if (!legacy) return null;

  const dismiss = () => {
    try {
      window.localStorage.setItem(dismissKey(sidecarId, legacy.sidecar_id), '1');
    } catch {
      // Storage can be unavailable (private mode); the notice then simply returns next visit.
    }
    setDismissedTick((n) => n + 1);
  };

  const data = preview.data;
  return (
    <aside
      className="mt-3 rounded-md border border-edge bg-surface-2 p-3"
      aria-label="Adopt legacy sidecar"
    >
      <p className="text-xs font-medium">Looks like the re-paired {legacy.sidecar_id}</p>
      <p className="text-xs text-fg-muted">
        Adopt its usage history and settings so this machine is one card again.
      </p>
      <div className="mt-2 flex gap-2">
        <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
          Review & adopt
        </Button>
        <Button size="sm" variant="ghost" onClick={dismiss}>
          Dismiss
        </Button>
      </div>

      <ResponsiveDialog
        open={open}
        onOpenChange={setOpen}
        title={`Adopt ${legacy.sidecar_id}?`}
        description="Its history and settings move to this machine and the old entry is removed. Nothing else changes."
      >
        <div className="mt-3 text-xs text-fg-muted" data-testid="adopt-preview">
          {preview.isError ? (
            <p className="text-danger">{preview.error.message}</p>
          ) : data ? (
            <ul className="list-disc space-y-1 pl-4">
              <li>{plural(data.events, 'usage event')} move to this machine</li>
              {data.pending_events > 0 ? (
                <li>{plural(data.pending_events, 'unassigned event')}</li>
              ) : null}
              {data.credential_tags > 0 ? (
                <li>{plural(data.credential_tags, 'credential tag')}</li>
              ) : null}
              {data.settings.length > 0 ? (
                <li>Settings carried over: {data.settings.join(', ')}</li>
              ) : null}
            </ul>
          ) : (
            <p>Checking what would move…</p>
          )}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)} disabled={adopt.isPending}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={() => adopt.mutate()}
            loading={adopt.isPending}
            disabled={!data}
          >
            Adopt
          </Button>
        </div>
      </ResponsiveDialog>
    </aside>
  );
}
