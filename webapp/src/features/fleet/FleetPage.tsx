// Fleet: sidecar registry — status, identity, tags, throughput, logs, and
// the pause/resume/rename/delete controls. Also hosts the
// silent-listener "Untagged credentials" banner + per-card badge (PR #288).

import { useEffect, useRef, useState } from 'react';
import { useLocation, useSearchParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleArrowUp, CircleAlert, Plus, RefreshCw, Server } from 'lucide-react';
import { toast } from 'sonner';
import {
  checkForUpdates,
  fetchSidecars,
  fetchUntaggedCredentials,
  triggerSidecarUpdate,
} from '@/api/endpoints';
import type { Sidecar, UntaggedCredential } from '@/api/types';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Skeleton, SkeletonRegion } from '@/components/ui/Skeleton';
import { AddSidecarCard } from './AddSidecarCard';
import { DeleteSidecarDialog } from './DeleteSidecarDialog';
import { isOnline } from './isOnline';
import { PendingUsageEventsCard } from './PendingUsageEventsCard';
import { buildSidecarNameMap } from './queries';
import { SidecarCard } from './SidecarCard';
import { SidecarSettingsDialog } from './SidecarSettingsDialog';
import { UntaggedBanner } from './UntaggedBanner';
import { UntaggedCredentialsDialog } from './UntaggedCredentialsDialog';
import { UpdateSidecarDialog } from './UpdateSidecarDialog';

export function FleetPage() {
  const queryClient = useQueryClient();
  const sidecars = useQuery({
    queryKey: ['fleet', 'sidecars'],
    queryFn: fetchSidecars,
    refetchInterval: 60_000,
  });
  // Silent-listener pending credentials (PR #288). Polled on the same
  // cadence as the sidecar list so the banner and per-card badge stay
  // current without forcing a fresh sidecar heartbeat on the wire.
  const untagged = useQuery({
    queryKey: ['fleet', 'untagged_credentials', 'all'],
    queryFn: () => fetchUntaggedCredentials(),
    refetchInterval: 60_000,
  });
  const [settingsId, setSettingsId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Sidecar | null>(null);
  const [updating, setUpdating] = useState<Sidecar | null>(null);
  const [confirmUpdateAll, setConfirmUpdateAll] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  // Silent-listener dialog selection. `undefined` means no single credential
  // is selected; `null` opens all pending credentials; an entry opens that row.
  const [tagDialogEntry, setTagDialogEntry] = useState<UntaggedCredential | null | undefined>(
    undefined,
  );
  // Set when the resolver should list every untagged credential of one sidecar.
  const [tagDialogSidecar, setTagDialogSidecar] = useState<string | null>(null);

  const updatable = (sidecars.data?.sidecars ?? []).filter((s) => s.update_available);

  // Deep links from other pages (`/fleet#sidecar-<id>`)
  // land on the sidecar's card once the list has loaded.
  // Scroll once per hash: the list refetches every minute and gets a new identity each time, so
  // keying on it would pull the page back to the card while the user is reading elsewhere.
  const { hash } = useLocation();
  const sidecarsLoaded = sidecars.data !== undefined;
  const scrolledHash = useRef<string | null>(null);
  useEffect(() => {
    if (!hash || !sidecarsLoaded || scrolledHash.current === hash) return;
    scrolledHash.current = hash;
    let id = hash.slice(1);
    try {
      id = decodeURIComponent(id);
    } catch {
      // A malformed escape: fall back to the raw fragment.
    }
    document.getElementById(id)?.scrollIntoView?.({ block: 'center' });
  }, [hash, sidecarsLoaded]);

  // `/fleet?settings=<id>` opens that sidecar's settings (the Credentials keep-alive note links
  // here). Once per value, so closing the dialog isn't undone by the next poll.
  const [searchParams, setSearchParams] = useSearchParams();
  const settingsParam = searchParams.get('settings');
  const handledSettings = useRef<string | null>(null);
  useEffect(() => {
    if (!settingsParam || !sidecarsLoaded || handledSettings.current === settingsParam) return;
    handledSettings.current = settingsParam;
    setSettingsId(settingsParam);
    setSearchParams(
      (prev) => {
        prev.delete('settings');
        return prev;
      },
      { replace: true },
    );
  }, [settingsParam, sidecarsLoaded, setSearchParams]);
  const settingsSidecar = (sidecars.data?.sidecars ?? []).find((s) => s.sidecar_id === settingsId) ?? null;

  // Force a GitHub release poll, then refresh both the sidecar badges and the
  // server-update banner (both read the same server-side cache).
  const check = useMutation({
    mutationFn: checkForUpdates,
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
      queryClient.invalidateQueries({ queryKey: ['system', 'settings'] });
      toast.success(
        res.update_available
          ? `Runway v${res.latest_version} is available`
          : `You're on the latest release (v${res.current_version})`,
      );
    },
    onError: (err) => toast.error(err.message),
  });

  // Fan the per-sidecar update endpoint out over every sidecar with a pending
  // update. allSettled so one failure doesn't abort the rest; we report the tally.
  const updateAll = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(
        updatable.map((s) => triggerSidecarUpdate(s.sidecar_id)),
      );
      const updated_count = results.filter((r) => r.status === 'fulfilled').length;
      return { updated_count, failed_count: results.length - updated_count };
    },
    onSuccess: ({ updated_count, failed_count }) => {
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
      if (failed_count === 0) {
        toast.success(`${updated_count} sidecar${updated_count === 1 ? '' : 's'} queued for update`);
      } else {
        toast.error(`Updated ${updated_count}, failed ${failed_count}`);
      }
      setConfirmUpdateAll(false);
    },
    onError: (err) => {
      toast.error(err.message);
      setConfirmUpdateAll(false);
    },
  });

  return (
    <>
      <PageHeader
        title="Fleet"
        description="Sidecar registry"
        actions={
          <div className="flex items-center gap-2">
            {/* The empty state already shows the card inline. */}
            {(sidecars.data?.sidecars.length ?? 0) > 0 ? (
              <Button
                size="sm"
                variant={showAdd ? 'primary' : 'secondary'}
                aria-expanded={showAdd}
                onClick={() => setShowAdd((v) => !v)}
              >
                <Plus className="size-3.5" aria-hidden />
                Add sidecar
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="secondary"
              onClick={() => check.mutate()}
              loading={check.isPending}
            >
              <RefreshCw className="size-3.5" aria-hidden />
              Check for updates
            </Button>
            {updatable.length > 0 ? (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => setConfirmUpdateAll(true)}
                loading={updateAll.isPending}
              >
                <CircleArrowUp className="size-3.5" aria-hidden />
                Update all ({updatable.length})
              </Button>
            ) : null}
          </div>
        }
      />
      <div className="p-4 lg:p-8">
        <PendingUsageEventsCard />
        {sidecars.isPending ? (
          <SkeletonRegion>
            <div className="grid gap-3 lg:grid-cols-2">
              <Skeleton className="h-40" />
              <Skeleton className="h-40" />
            </div>
          </SkeletonRegion>
        ) : sidecars.isError ? (
          // A failed request is not an empty fleet. Reporting "No sidecars
          // yet" for a network error tells the user to install something
          // they already have.
          <EmptyState
            icon={CircleAlert}
            title="Could not load the sidecars"
            description={sidecars.error.message}
            action={<Button size="sm" onClick={() => sidecars.refetch()}>Retry</Button>}
          />
        ) : (sidecars.data?.sidecars.length ?? 0) === 0 ? (
          <>
            <EmptyState
              icon={Server}
              title="No sidecars yet"
              description="Install the Runway sidecar on a machine you work from; it will register here on its first check-in."
            />
            <AddSidecarCard className="mx-auto max-w-2xl" />
            {/* "All machines" rules outlive the last sidecar and apply to the next
                one; they live under Settings → Credentials → Rules. */}
          </>
        ) : (
          <>
            {showAdd ? <AddSidecarCard className="mb-4" /> : null}
            <UntaggedBanner
              counts={untagged.data?.counts_by_sidecar ?? {}}
              names={buildSidecarNameMap(sidecars.data?.sidecars ?? [])}
              loading={untagged.isPending}
              onResolveAll={() => {
                setTagDialogSidecar(null);
                setTagDialogEntry(null);
              }}
              onResolveSidecar={(sidecarId) => {
                setTagDialogEntry(undefined);
                setTagDialogSidecar(sidecarId);
              }}
            />
            <div className="grid gap-3 lg:grid-cols-2">
              {sidecars.data!.sidecars.map((s) => (
                <SidecarCard
                  key={s.sidecar_id}
                  sidecar={s}
                  untaggedCount={untagged.data?.counts_by_sidecar[s.sidecar_id] ?? 0}
                  onSettings={() => setSettingsId(s.sidecar_id)}
                  onUpdate={() => setUpdating(s)}
                  onResolveUntagged={() => {
                    setTagDialogEntry(undefined);
                    setTagDialogSidecar(s.sidecar_id);
                  }}
                />
              ))}
            </div>
          </>
        )}
      </div>
      <SidecarSettingsDialog
        sidecar={settingsSidecar}
        online={settingsSidecar ? isOnline(settingsSidecar) : false}
        onClose={() => setSettingsId(null)}
        onUpdate={(sc) => {
          setSettingsId(null);
          setUpdating(sc);
        }}
        onDelete={(sc) => {
          setSettingsId(null);
          setDeleting(sc);
        }}
      />
      <DeleteSidecarDialog sidecar={deleting} onClose={() => setDeleting(null)} />
      <UpdateSidecarDialog sidecar={updating} onClose={() => setUpdating(null)} />
      <UntaggedCredentialsDialog
        open={tagDialogEntry !== undefined || tagDialogSidecar !== null}
        singleEntry={tagDialogEntry ?? undefined}
        sidecarId={tagDialogSidecar ?? undefined}
        onClose={() => {
          setTagDialogSidecar(null);
          setTagDialogEntry(undefined);
        }}
      />
      <ResponsiveDialog
        open={confirmUpdateAll}
        onOpenChange={(open) => {
          if (!open) setConfirmUpdateAll(false);
        }}
        title="Push update to all?"
        description={`${updatable.length} sidecar${updatable.length === 1 ? '' : 's'} with a pending update`}
      >
        <p className="text-sm text-fg-muted">
          Queues the latest build for{' '}
          {updatable.map((s) => s.custom_name || s.hostname || s.sidecar_id).join(', ')}. Each
          downloads, verifies, and installs the update on its next check-in, then restarts itself.
          Collection resumes automatically.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={() => setConfirmUpdateAll(false)}>Cancel</Button>
          <Button
            variant="primary"
            onClick={() => updateAll.mutate()}
            loading={updateAll.isPending}
          >
            Update all
          </Button>
        </div>
      </ResponsiveDialog>
    </>
  );
}
