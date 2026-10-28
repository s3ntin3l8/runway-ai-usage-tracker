// Offers to merge a machine's two Fleet entries after it was re-paired.
//
// Re-pairing enrols a new paired device and leaves the old shared-key entry behind, so one machine
// shows up twice. The merge always goes the same way, from the old entry into the paired device:
// the old entry's history and settings move over and the old entry disappears. It is offered on
// both cards (this component works out which one it is on) and says so, because both entries
// usually carry the same hostname. Nothing merges without a click: a hostname is a label, not proof.

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight } from 'lucide-react';
import { toast } from 'sonner';
import { adoptLegacySidecar } from '@/api/endpoints';
import type { Sidecar, SidecarDescriptor } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { formatCount, timeAgo } from '@/lib/format';

const DEVICE_ID = /^device-[a-f0-9]{32}$/;

interface Pair {
  deviceId: string;
  legacyId: string;
  other: SidecarDescriptor;
}

const dismissKey = (pair: Pair) => `runway.fleet.merge-dismissed.${pair.deviceId}.${pair.legacyId}`;

function isDismissed(pair: Pair): boolean {
  try {
    return window.localStorage.getItem(dismissKey(pair)) === '1';
  } catch {
    return false;
  }
}

const plural = (n: number, noun: string) => `${formatCount(n)} ${noun}${n === 1 ? '' : 's'}`;

export function MergeSidecarNotice({ sidecar }: { sidecar: Sidecar }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [, setDismissedTick] = useState(0);

  const isDevice = DEVICE_ID.test(sidecar.sidecar_id);
  const pairs: Pair[] = isDevice
    ? (sidecar.adoptable_legacy ?? []).map((other) => ({
        deviceId: sidecar.sidecar_id,
        legacyId: other.sidecar_id,
        other,
      }))
    : (sidecar.replaced_by ?? []).map((other) => ({
        deviceId: other.sidecar_id,
        legacyId: sidecar.sidecar_id,
        other,
      }));
  const pair = pairs.find((candidate) => !isDismissed(candidate));

  // The preview is read only when the dialog opens, so an idle Fleet page costs nothing.
  const preview = useQuery({
    queryKey: ['fleet', 'adopt-preview', pair?.deviceId, pair?.legacyId],
    queryFn: () => adoptLegacySidecar(pair!.deviceId, pair!.legacyId, true),
    enabled: open && pair !== undefined,
  });

  const merge = useMutation({
    mutationFn: () => adoptLegacySidecar(pair!.deviceId, pair!.legacyId, false),
    onSuccess: () => {
      toast.success(`Merged ${pair?.other.name ?? 'the old entry'} into the paired device`);
      setOpen(false);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });

  if (!pair) return null;

  const dismiss = () => {
    try {
      window.localStorage.setItem(dismissKey(pair), '1');
    } catch {
      // Storage can be unavailable (private mode); the notice then simply returns next visit.
    }
    setDismissedTick((n) => n + 1);
  };

  const data = preview.data;
  return (
    <aside
      className="mt-3 rounded-md border border-edge bg-surface-2 p-3"
      aria-label="Merge the old entry into the paired device"
    >
      {isDevice ? (
        <>
          <p className="text-xs font-medium">Same machine as the old entry “{pair.other.name}”?</p>
          <p className="text-xs text-fg-muted">
            That entry went quiet when this paired device appeared. Merge it into this card to keep its history
            and settings; the old entry is removed.
          </p>
        </>
      ) : (
        <>
          <p className="text-xs font-medium">This machine was re-paired as “{pair.other.name}”.</p>
          <p className="text-xs text-fg-muted">
            Merge this old entry into the paired device to keep its history and settings. This entry is removed
            and the paired device stays.
          </p>
        </>
      )}
      <div className="mt-2 flex gap-2">
        <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
          Review merge
        </Button>
        <Button size="sm" variant="ghost" onClick={dismiss}>
          Dismiss
        </Button>
      </div>

      <ResponsiveDialog
        open={open}
        onOpenChange={setOpen}
        title="Merge into the paired device?"
        description="Everything from the old entry moves onto the paired device, then the old entry is removed."
      >
        <div data-testid="merge-preview" className="text-xs text-fg-muted">
          {preview.isError ? (
            <p className="text-danger">{preview.error.message}</p>
          ) : data?.from && data.into ? (
            <>
              <div className="grid items-stretch gap-2 sm:grid-cols-[1fr_auto_1fr]">
                <Entry heading="Old entry · shared key" entry={data.from} />
                <ArrowRight className="mx-auto size-4 self-center text-fg-subtle" aria-label="merges into" />
                <Entry heading="Paired device" entry={data.into} emphasised />
              </div>
              <ul className="mt-3 list-disc space-y-1 pl-4">
                <li>{plural(data.events, 'usage event')} move to the paired device</li>
                {data.pending_events > 0 ? <li>{plural(data.pending_events, 'unassigned event')}</li> : null}
                {data.credential_tags > 0 ? <li>{plural(data.credential_tags, 'credential tag')}</li> : null}
                {data.settings.length > 0 ? <li>Settings carried over: {data.settings.join(', ')}</li> : null}
                <li>
                  Pushes: {formatCount(data.into.ingest_count)} + {formatCount(data.from.ingest_count)} ={' '}
                  <span className="font-mono text-fg">{formatCount(data.pushes_after)}</span>
                </li>
              </ul>
            </>
          ) : (
            <p>Checking what would move…</p>
          )}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)} disabled={merge.isPending}>
            Cancel
          </Button>
          <Button size="sm" onClick={() => merge.mutate()} loading={merge.isPending} disabled={!data?.from}>
            Merge into paired device
          </Button>
        </div>
      </ResponsiveDialog>
    </aside>
  );
}

function Entry({
  heading,
  entry,
  emphasised = false,
}: {
  heading: string;
  entry: SidecarDescriptor;
  emphasised?: boolean;
}) {
  return (
    <div
      className={`min-w-0 rounded-md border p-2 ${emphasised ? 'border-accent/40 bg-surface-2' : 'border-edge'}`}
    >
      <p className="text-[10px] font-medium uppercase tracking-wide text-fg-subtle">{heading}</p>
      <p className="mt-0.5 truncate text-[13px] font-medium text-fg">{entry.name}</p>
      {entry.name !== entry.sidecar_id ? (
        <p className="truncate font-mono text-[10px] text-fg-subtle" title={entry.sidecar_id}>
          {entry.sidecar_id}
        </p>
      ) : null}
      <p className="mt-1">
        last seen {timeAgo(entry.last_seen)} · {plural(entry.ingest_count, 'push')}
      </p>
    </div>
  );
}
