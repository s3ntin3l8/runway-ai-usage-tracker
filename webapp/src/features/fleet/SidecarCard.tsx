import { useState } from 'react';
import { Link } from 'react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { CircleArrowUp, Pause, Play, Settings } from 'lucide-react';
import { toast } from 'sonner';
import { setSidecarEnabled } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { StatusDot } from '@/components/ui/StatusDot';
import { timeAgo } from '@/lib/format';
import { isOnline } from './isOnline';
import { MergeSidecarNotice } from './MergeSidecarNotice';
import { SidecarSourceNotice } from './SidecarSources';
import { useSidecarKeepAlive } from './SidecarSettingsDialog';
import { SourceSyncNotice } from './SourceSyncNotice';

export function SidecarCard({
  sidecar,
  untaggedCount,
  onSettings,
  onUpdate,
  onResolveUntagged,
}: {
  sidecar: Sidecar;
  untaggedCount: number;
  onSettings: () => void;
  onUpdate: () => void;
  /** Open the resolver for *all* of this sidecar's untagged credentials. */
  onResolveUntagged: () => void;
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

  const ka = useSidecarKeepAlive(sidecar, online);
  const keepAliveReported = sidecar.keep_alive === true;

  const logs = (sidecar.last_log_lines ?? []).filter(Boolean);

  return (
    <Card id={`sidecar-${sidecar.sidecar_id}`} className="scroll-mt-20 p-4">
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
            </div>
          </div>

          {(sidecar.tags?.length ?? 0) > 0 ||
          paused ||
          keepAliveReported ||
          ka.pending ||
          untaggedCount > 0 ||
          sidecar.update_channel_desired ||
          sidecar.auto_update_desired === false ||
          sidecar.log_level_desired === 'DEBUG' ? (
            <div className="mt-2.5 flex flex-wrap gap-1">
              {paused ? <Badge variant="warning">paused</Badge> : null}
              {ka.pending ? (
                <Badge variant="warning" title={ka.status}>
                  keep-alive {ka.effective ? 'on' : 'off'} pending
                </Badge>
              ) : keepAliveReported ? (
                <Badge variant="neutral">keep-alive</Badge>
              ) : null}
              {sidecar.update_channel_desired ? (
                <Badge variant="neutral" title="Pinned to this update channel in Settings">
                  {sidecar.update_channel_desired}
                </Badge>
              ) : null}
              {sidecar.log_level_desired === 'DEBUG' ? (
                <Badge variant="warning" title="Verbose logging was switched on for this sidecar in Settings">
                  debug logging
                </Badge>
              ) : null}
              {/* Only "off" is badged: "on" is the common case and just follows the fleet default. */}
              {sidecar.auto_update_desired === false ? (
                <Badge variant="neutral" title="Auto-update is turned off for this sidecar">
                  no auto-update
                </Badge>
              ) : null}
              {(sidecar.tags ?? []).map((tag) => (
                <Badge key={tag} variant="neutral">
                  {tag}
                </Badge>
              ))}
              {untaggedCount > 0 ? (
                <button
                  type="button"
                  onClick={onResolveUntagged}
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

          {!ka.unsupported ? (
            <p
              className={`mt-3 text-[11px] ${ka.pending ? 'text-warning' : 'text-fg-muted'}`}
              data-testid="keep-alive-status"
            >
              {ka.status}
            </p>
          ) : null}

          <dl className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
            <div className="col-span-3 min-w-0">
              <dt className="text-fg-subtle">Version</dt>
              <dd className="mt-0.5 flex flex-wrap items-center gap-1.5">
                <span className="font-mono tabular">v{sidecar.sidecar_version ?? '?'}</span>
                {sidecar.channel === 'beta' || sidecar.channel === 'edge' ? (
                  <Badge
                    variant="accent"
                    className="uppercase tracking-wide"
                    title={sidecar.channel === 'beta' ? 'Numbered prerelease channel' : 'Rolling prerelease channel'}
                  >
                    {sidecar.channel}
                  </Badge>
                ) : null}
                {sidecar.update_available ? <Badge variant="warning">update</Badge> : null}
                {!sidecar.update_available && sidecar.outdated ? (
                  <Badge variant="neutral" title="A newer build exists; it installs once the sidecar is back online">
                    outdated
                  </Badge>
                ) : null}
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

          <div className="mt-3 flex items-center justify-between gap-2 text-[11px]">
            <span className="text-fg-subtle">Credentials & account identities</span>
            <Link
              to={`/settings/credentials?view=machine#machine-${encodeURIComponent(sidecar.sidecar_id)}`}
              className="font-medium text-accent-text hover:underline"
            >
              View
            </Link>
          </div>

          {sidecar.adoptable_legacy?.length || sidecar.replaced_by?.length ? (
            <MergeSidecarNotice sidecar={sidecar} />
          ) : null}
          <SourceSyncNotice sidecar={sidecar} online={online} />
          <SidecarSourceNotice sidecarId={sidecar.sidecar_id} osPlatform={sidecar.os_platform} onOpen={onSettings} />
          <div className="mt-3 flex items-center gap-2">
            <Button size="sm" variant="secondary" onClick={onSettings}>
              <Settings className="size-3.5" aria-hidden />
              Settings
            </Button>
            {logs.length > 0 ? (
              <Button size="sm" variant="ghost" onClick={() => setShowLogs(true)}>
                Logs
              </Button>
            ) : null}
            {sidecar.update_available ? (
              <Button size="sm" variant="secondary" onClick={onUpdate}>
                <CircleArrowUp className="size-3.5" aria-hidden />
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
