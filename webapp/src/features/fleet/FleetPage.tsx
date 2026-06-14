// Fleet: sidecar registry — status, identity, tags, throughput, logs, and
// the pause/resume/rename/delete controls.

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowUpCircle, Pause, Pencil, Play, RefreshCw, Server, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import {
  checkForUpdates,
  deleteSidecar,
  fetchSidecars,
  patchSidecar,
  setSidecarEnabled,
  triggerSidecarUpdate,
} from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
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

const ONLINE_THRESHOLD_MS = 30 * 60_000;

function isOnline(s: Sidecar): boolean {
  if (!s.last_seen) return false;
  return Date.now() - Date.parse(s.last_seen) < ONLINE_THRESHOLD_MS;
}

export function FleetPage() {
  const queryClient = useQueryClient();
  const sidecars = useQuery({
    queryKey: ['fleet', 'sidecars'],
    queryFn: fetchSidecars,
    refetchInterval: 60_000,
  });
  const [editing, setEditing] = useState<Sidecar | null>(null);
  const [deleting, setDeleting] = useState<Sidecar | null>(null);
  const [updating, setUpdating] = useState<Sidecar | null>(null);

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

  return (
    <>
      <PageHeader
        title="Fleet"
        description="Sidecar registry"
        actions={
          <Button
            size="sm"
            variant="secondary"
            onClick={() => check.mutate()}
            loading={check.isPending}
          >
            <RefreshCw className="size-3.5" aria-hidden />
            Check for updates
          </Button>
        }
      />
      <div className="p-4 lg:p-8">
        {sidecars.isPending ? (
          <div className="grid gap-3 lg:grid-cols-2">
            <Skeleton className="h-40" />
            <Skeleton className="h-40" />
          </div>
        ) : (sidecars.data?.sidecars.length ?? 0) === 0 ? (
          <EmptyState
            icon={Server}
            title="No sidecars yet"
            description="Install the Runway sidecar on a machine you work from; it will register here on its first check-in."
          />
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {sidecars.data!.sidecars.map((s) => (
              <SidecarCard
                key={s.sidecar_id}
                sidecar={s}
                onEdit={() => setEditing(s)}
                onDelete={() => setDeleting(s)}
                onUpdate={() => setUpdating(s)}
              />
            ))}
          </div>
        )}
      </div>
      <EditSidecarDialog sidecar={editing} onClose={() => setEditing(null)} />
      <DeleteSidecarDialog sidecar={deleting} onClose={() => setDeleting(null)} />
      <UpdateSidecarDialog sidecar={updating} onClose={() => setUpdating(null)} />
    </>
  );
}

function SidecarCard({
  sidecar,
  onEdit,
  onDelete,
  onUpdate,
}: {
  sidecar: Sidecar;
  onEdit: () => void;
  onDelete: () => void;
  onUpdate: () => void;
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

          {(sidecar.tags?.length ?? 0) > 0 || paused ? (
            <div className="mt-2.5 flex flex-wrap gap-1">
              {paused ? <Badge variant="warning">paused</Badge> : null}
              {(sidecar.tags ?? []).map((tag) => (
                <Badge key={tag} variant="neutral">
                  {tag}
                </Badge>
              ))}
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
