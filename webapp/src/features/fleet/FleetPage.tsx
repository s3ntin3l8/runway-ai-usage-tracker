// Fleet: sidecar registry — status, identity, tags, throughput, logs, and
// the pause/resume/rename/delete controls. Also hosts the
// silent-listener "Untagged credentials" banner + per-card badge (PR #288).

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowUpCircle,
  Pause,
  Plus,
  Pencil,
  Play,
  RefreshCw,
  Server,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  checkForUpdates,
  deleteSidecar,
  fetchSidecars,
  fetchUntaggedCredentials,
  patchSidecar,
  setSidecarEnabled,
  triggerSidecarUpdate,
} from '@/api/endpoints';
import type { Sidecar, UntaggedCredential } from '@/api/types';
import { PageHeader } from '@/components/layout/PageHeader';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatusDot } from '@/components/ui/StatusDot';
import { timeAgo } from '@/lib/format';
import { AddSidecarCard } from './AddSidecarCard';
import { UntaggedCredentialsDialog } from './UntaggedCredentialsDialog';

// Liveness is computed server-side (fleet_registry.to_dict's `stale` field,
// gated on stale_threshold_minutes) so there's one source of truth — a
// second, differently-tuned client-side threshold previously disagreed with
// it (30min here vs. the server's 60min), which could show a sidecar as
// "stale" in this badge while the server still offered it "update
// available". `stale` is always present in a live API response; `!s.stale`
// only defaults to online when it's genuinely absent (e.g. a stale test
// fixture).
function isOnline(s: Sidecar): boolean {
  if (!s.last_seen) return false;
  return !s.stale;
}

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
  const [editing, setEditing] = useState<Sidecar | null>(null);
  const [deleting, setDeleting] = useState<Sidecar | null>(null);
  const [updating, setUpdating] = useState<Sidecar | null>(null);
  const [confirmUpdateAll, setConfirmUpdateAll] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  // Silent-listener dialog state. ``null`` = closed; an
  // ``UntaggedCredential`` = single-row entry (per-card badge); ``undefined`` =
  // open in banner mode (all pending rows for the operator).
  const [tagDialogEntry, setTagDialogEntry] = useState<UntaggedCredential | null | undefined>(
    undefined,
  );

  const updatable = (sidecars.data?.sidecars ?? []).filter((s) => s.update_available);

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
                <ArrowUpCircle className="size-3.5" aria-hidden />
                Update all ({updatable.length})
              </Button>
            ) : null}
          </div>
        }
      />
      <div className="p-4 lg:p-8">
        {sidecars.isPending ? (
          <div className="grid gap-3 lg:grid-cols-2">
            <Skeleton className="h-40" />
            <Skeleton className="h-40" />
          </div>
        ) : (sidecars.data?.sidecars.length ?? 0) === 0 ? (
          <>
            <EmptyState
              icon={Server}
              title="No sidecars yet"
              description="Install the Runway sidecar on a machine you work from; it will register here on its first check-in."
            />
            <AddSidecarCard className="mx-auto max-w-2xl" />
          </>
        ) : (
          <>
            {showAdd ? <AddSidecarCard className="mb-4" /> : null}
            <UntaggedBanner
              counts={untagged.data?.counts_by_sidecar ?? {}}
              items={untagged.data?.items ?? []}
              loading={untagged.isPending}
              onResolveAll={() => setTagDialogEntry(null)}
              onResolveOne={(entry) => setTagDialogEntry(entry)}
            />
            <div className="grid gap-3 lg:grid-cols-2">
              {sidecars.data!.sidecars.map((s) => (
                <SidecarCard
                  key={s.sidecar_id}
                  sidecar={s}
                  untaggedCount={untagged.data?.counts_by_sidecar[s.sidecar_id] ?? 0}
                  untaggedEntries={
                    (untagged.data?.items ?? []).filter((e) => e.sidecar_id === s.sidecar_id)
                  }
                  onEdit={() => setEditing(s)}
                  onDelete={() => setDeleting(s)}
                  onUpdate={() => setUpdating(s)}
                  onResolveUntagged={(entry) => setTagDialogEntry(entry)}
                />
              ))}
            </div>
          </>
        )}
      </div>
      <EditSidecarDialog sidecar={editing} onClose={() => setEditing(null)} />
      <DeleteSidecarDialog sidecar={deleting} onClose={() => setDeleting(null)} />
      <UpdateSidecarDialog sidecar={updating} onClose={() => setUpdating(null)} />
      <UntaggedCredentialsDialog
        open={tagDialogEntry !== undefined}
        singleEntry={tagDialogEntry ?? undefined}
        onClose={() => setTagDialogEntry(undefined)}
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

function SidecarCard({
  sidecar,
  untaggedCount,
  untaggedEntries,
  onEdit,
  onDelete,
  onUpdate,
  onResolveUntagged,
}: {
  sidecar: Sidecar;
  untaggedCount: number;
  untaggedEntries: UntaggedCredential[];
  onEdit: () => void;
  onDelete: () => void;
  onUpdate: () => void;
  onResolveUntagged: (entry: UntaggedCredential) => void;
}) {
  const queryClient = useQueryClient();
  const online = isOnline(sidecar);
  const paused = sidecar.collection_enabled === false;
  const [showLogs, setShowLogs] = useState(false);

  const toggle = useMutation({
    mutationFn: () => setSidecarEnabled(sidecar.sidecar_id, paused),
    onSuccess: () => {
      toast.success(paused ? 'Sidecar resumed' : 'Sidecar paused');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });

  const logs = (sidecar.last_log_lines ?? []).filter(Boolean);

  return (
    <Card className="p-4">
      {/* Status dot is a leading rail so the title, metrics, and footer all
          share one left edge. */}
      <div className="flex items-start gap-2.5">
        <StatusDot
          status={paused ? 'unknown' : online ? 'ok' : 'warning'}
          label={paused ? 'paused' : online ? 'online' : 'stale'}
          className="mt-1.5"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-[13px] font-semibold">
                {sidecar.custom_name || sidecar.hostname || sidecar.sidecar_id}
              </p>
              <p className="truncate text-[11px] text-fg-subtle">
                {sidecar.hostname && sidecar.custom_name ? `${sidecar.hostname} · ` : ''}
                {sidecar.os_platform ?? '—'}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={paused ? 'Resume collection' : 'Pause collection'}
                title={paused ? 'Resume collection' : 'Pause collection'}
                onClick={() => toggle.mutate()}
                loading={toggle.isPending}
              >
                {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label="Delete sidecar"
                title="Delete sidecar"
                onClick={onDelete}
                className="text-critical hover:bg-critical-muted"
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          </div>

          {(sidecar.tags?.length ?? 0) > 0 || paused || untaggedCount > 0 ? (
            <div className="mt-2.5 flex flex-wrap gap-1">
              {paused ? <Badge variant="warning">paused</Badge> : null}
              {(sidecar.tags ?? []).map((tag) => (
                <Badge key={tag} variant="neutral">
                  {tag}
                </Badge>
              ))}
              {untaggedCount > 0 ? (
                <button
                  type="button"
                  onClick={() => onResolveUntagged(untaggedEntries[0])}
                  className="rounded-md border border-warning/40 bg-warning-muted px-2 py-0.5 text-[11px] font-medium text-warning hover:border-warning"
                  aria-label={`${untaggedCount} untagged credential${
                    untaggedCount === 1 ? '' : 's'
                  } — click to resolve`}
                  title={`${untaggedCount} credential${
                    untaggedCount === 1 ? '' : 's'
                  } waiting for an operator tag`}
                >
                  Untagged: {untaggedCount}
                </button>
              ) : null}
            </div>
          ) : null}

          <dl className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
            <div className="col-span-3 min-w-0">
              <dt className="text-fg-subtle">Version</dt>
              <dd className="mt-0.5 flex flex-wrap items-center gap-1.5">
                <span className="font-mono tabular">v{sidecar.sidecar_version ?? '?'}</span>
                {sidecar.channel === 'edge' ? (
                  <Badge
                    variant="accent"
                    className="uppercase tracking-wide"
                    title="Rolling prerelease channel"
                  >
                    edge
                  </Badge>
                ) : null}
                {sidecar.update_available ? <Badge variant="warning">update</Badge> : null}
              </dd>
            </div>
            <div>
              <dt className="text-fg-subtle">Last seen</dt>
              <dd className="mt-0.5 font-mono tabular">{timeAgo(sidecar.last_seen)}</dd>
            </div>
            <div>
              <dt className="text-fg-subtle">Pushes</dt>
              <dd className="mt-0.5 font-mono tabular">{sidecar.ingest_count ?? 0}</dd>
            </div>
            <div>
              <dt className="text-fg-subtle">Errors</dt>
              <dd className={`mt-0.5 font-mono tabular ${(sidecar.error_count ?? 0) > 0 ? 'text-warning' : ''}`}>
                {sidecar.error_count ?? 0}
              </dd>
            </div>
          </dl>

          <div className="mt-3 flex items-center gap-2">
            <Button size="sm" variant="secondary" onClick={onEdit}>
              <Pencil className="size-3.5" aria-hidden />
              Rename / tags
            </Button>
            {logs.length > 0 ? (
              <Button size="sm" variant="ghost" onClick={() => setShowLogs(true)}>
                Logs
              </Button>
            ) : null}
            {sidecar.update_available ? (
              <Button size="sm" variant="secondary" onClick={onUpdate}>
                <ArrowUpCircle className="size-3.5" aria-hidden />
                Update now
              </Button>
            ) : null}
          </div>
        </div>
      </div>

      <ResponsiveDialog
        open={showLogs}
        onOpenChange={setShowLogs}
        title={`Logs — ${sidecar.custom_name || sidecar.hostname || sidecar.sidecar_id}`}
        width="max-w-2xl"
      >
        <pre className="max-h-96 overflow-auto rounded-sm bg-surface-2 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
          {logs.join('\n')}
        </pre>
      </ResponsiveDialog>
    </Card>
  );
}

function EditSidecarDialog({ sidecar, onClose }: { sidecar: Sidecar | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  // Remount the form whenever a different sidecar opens
  return (
    <ResponsiveDialog
      open={sidecar !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Edit sidecar"
      description={sidecar?.hostname}
    >
      {sidecar ? (
        <EditSidecarForm
          key={sidecar.sidecar_id}
          sidecar={sidecar}
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
            onClose();
          }}
        />
      ) : null}
    </ResponsiveDialog>
  );
}

function EditSidecarForm({ sidecar, onSaved }: { sidecar: Sidecar; onSaved: () => void }) {
  const [name, setName] = useState(sidecar.custom_name ?? '');
  const [tags, setTags] = useState((sidecar.tags ?? []).join(', '));

  const save = useMutation({
    mutationFn: () =>
      patchSidecar(sidecar.sidecar_id, {
        custom_name: name.trim(),
        tags: tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      toast.success('Sidecar updated');
      onSaved();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="flex flex-col gap-3"
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-name">Display name</Label>
        <Input
          id="sidecar-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={sidecar.hostname}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-tags">Tags (comma-separated)</Label>
        <Input
          id="sidecar-tags"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="work, laptop"
        />
      </div>
      <Button type="submit" variant="primary" className="mt-1" loading={save.isPending}>
        Save
      </Button>
    </form>
  );
}

function DeleteSidecarDialog({
  sidecar,
  onClose,
}: {
  sidecar: Sidecar | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const del = useMutation({
    mutationFn: (id: string) => deleteSidecar(id),
    onSuccess: () => {
      toast.success('Sidecar removed');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <ResponsiveDialog
      open={sidecar !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Remove sidecar?"
      description={sidecar?.custom_name || sidecar?.hostname || sidecar?.sidecar_id}
    >
      <p className="text-sm text-fg-muted">
        The registry entry is removed; collected usage events stay. The sidecar re-registers if it
        keeps running and checks in again.
      </p>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="danger"
          onClick={() => sidecar && del.mutate(sidecar.sidecar_id)}
          loading={del.isPending}
        >
          Remove
        </Button>
      </div>
    </ResponsiveDialog>
  );
}

function UpdateSidecarDialog({
  sidecar,
  onClose,
}: {
  sidecar: Sidecar | null;
  onClose: () => void;
}) {
  const update = useMutation({
    mutationFn: (id: string) => triggerSidecarUpdate(id),
    onSuccess: () => {
      toast.success('Update pushed — the sidecar installs it on its next check-in');
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <ResponsiveDialog
      open={sidecar !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Push update?"
      description={sidecar?.custom_name || sidecar?.hostname || sidecar?.sidecar_id}
    >
      <p className="text-sm text-fg-muted">
        Queues the latest build for this sidecar. It downloads, verifies, and installs the
        update on its next check-in, then restarts itself. Collection resumes automatically.
      </p>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="primary"
          onClick={() => sidecar && update.mutate(sidecar.sidecar_id)}
          loading={update.isPending}
        >
          Update
        </Button>
      </div>
    </ResponsiveDialog>
  );
}

function UntaggedBanner({
  counts,
  items,
  loading,
  onResolveAll,
  onResolveOne,
}: {
  counts: Record<string, number>;
  items: UntaggedCredential[];
  loading: boolean;
  onResolveAll: () => void;
  onResolveOne: (entry: UntaggedCredential) => void;
}) {
  // Banner is hidden when nothing pending — same render path either way
  // so the parent doesn't have to conditionalize.
  const totalCount = Object.values(counts).reduce((acc, n) => acc + n, 0);
  if (totalCount === 0 && !loading) return null;

  return (
    <Card className="mb-3 border-warning/40 bg-warning-muted p-3">
      <div className="flex items-start gap-3">
        <TriangleAlert
          className="mt-0.5 size-4 shrink-0 text-warning"
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-semibold">
            Untagged credentials
            <span className="ml-2 font-mono text-[11px] text-fg-muted">
              {totalCount} pending
            </span>
          </p>
          <p className="mt-0.5 text-[11px] text-fg-muted">
            Sidecars are blocking these from upload until you map each to a configured provider row.
          </p>

          {Object.keys(counts).length > 0 ? (
            <ul className="mt-2 flex flex-wrap gap-1">
              {Object.entries(counts).map(([sidecarId, count]) => {
                const sidecarItems = items.filter((e) => e.sidecar_id === sidecarId);
                return (
                  <li key={sidecarId}>
                    <button
                      type="button"
                      onClick={() => sidecarItems[0] && onResolveOne(sidecarItems[0])}
                      className="rounded-md border border-warning/40 bg-surface-1 px-2 py-0.5 text-[11px] font-medium text-fg hover:border-warning"
                      aria-label={`${count} untagged credential${
                        count === 1 ? '' : 's'
                      } on sidecar ${sidecarId} — click to resolve`}
                      title={`${sidecarId}: ${count} pending`}
                    >
                      <span className="font-mono">{sidecarId}</span> · {count}
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>

        <Button size="sm" variant="primary" onClick={onResolveAll}>
          Tag now
        </Button>
      </div>
    </Card>
  );
}
