// The Keychain (macOS) / credential (Windows) sources card in a sidecar's settings: a read-and-steer
// view. Permission itself is only ever granted on the device; nothing here grants consent.

import { Fragment, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Info, TriangleAlert } from 'lucide-react';
import { toast } from 'sonner';
import { commandSidecarSource } from '@/api/endpoints';
import type { FleetSourceAction, FleetSourceStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Callout } from '@/components/ui/Callout';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';
import { CollectionBadge, MacBadge, ServiceCollection } from './sourceBadges';
import { type Actions, SourceDetails, SourceMenu } from './sourceMenu';
import {
  countSources,
  groupSources,
  isShown,
  macStatus,
  needsAuthorization,
  rollup,
  type ServiceGroup,
  type SourceFilter,
  sourceBrowser,
} from './sourceModel';
import { useSidecarSources } from './sourceQueries';
import { STATUS } from './sourceStatus';
import { Caret, Tile } from './sourceWidgets';

const GRID =
  'grid grid-cols-[14px_minmax(0,1fr)_104px_118px_22px] items-center gap-x-2 max-sm:grid-cols-[14px_minmax(0,1fr)_104px_22px]';

export function SidecarSources({ sidecarId, osPlatform }: { sidecarId: string; osPlatform?: string | null }) {
  const queryClient = useQueryClient();
  const { macOS, compatible, enrolled, query } = useSidecarSources(sidecarId, osPlatform);
  const [filter, setFilter] = useState<SourceFilter | null>(null);
  const [showOff, setShowOff] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [revoking, setRevoking] = useState<FleetSourceStatus | null>(null);

  const done = () => queryClient.invalidateQueries({ queryKey: ['fleet', 'sources', sidecarId] });
  const command = useMutation({
    mutationFn: ({ source, action }: { source: FleetSourceStatus; action: FleetSourceAction }) =>
      commandSidecarSource(sidecarId, source.source_id, action),
    onSuccess: () => {
      done();
      toast.success('Source command recorded');
    },
    onError: (error) => toast.error(error.message),
  });
  const bulk = useMutation({
    mutationFn: ({ targets, action }: { targets: FleetSourceStatus[]; action: FleetSourceAction }) =>
      Promise.all(targets.map((source) => commandSidecarSource(sidecarId, source.source_id, action))),
    onSuccess: (_data, { targets }) => {
      done();
      toast.success(`Source command recorded for ${targets.length} source${targets.length === 1 ? '' : 's'}`);
    },
    onError: (error) => toast.error(error.message),
  });

  if (!compatible) return null;
  const heading = macOS ? 'Keychain sources' : 'Credential sources';
  const device = macOS ? 'Mac' : 'device';
  if (!enrolled)
    return macOS ? (
      <section aria-label={heading} className="flex flex-col gap-2 rounded-lg border border-edge p-4">
        <h3 className="text-[13px] font-semibold">{heading}</h3>
        <p className="text-xs text-fg-muted">
          Limited: pair this sidecar again to enroll its device identity. Existing file and environment sources keep
          working.
        </p>
      </section>
    ) : null;

  const sources = query.data?.sources ?? [];
  // A Windows or Linux device that reports nothing has nothing to show; the card appears once it does.
  if (!macOS && sources.length === 0) return null;

  const commands = query.data?.commands ?? [];
  const counts = countSources(sources);
  const busy = command.isPending || bulk.isPending;
  const actions: Actions = {
    busy,
    macOS,
    run: (source, action) => command.mutate({ source, action }),
    revoke: setRevoking,
  };
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  const pick = (next: SourceFilter) => setFilter((current) => (current === next ? null : next));

  const { apps, services } = groupSources(sources);
  const shownApps = apps.filter((s) => isShown(s, filter, showOff));
  const shownServices = services
    .map((g): ServiceGroup => ({ ...g, sources: g.sources.filter((s) => isShown(s, filter, showOff)) }))
    .filter((g) => g.sources.length > 0);

  const needAuth = sources.filter((s) => s.read && needsAuthorization(s));
  const fresh = sources.filter((s) => s.announcement);
  const pausable = sources.filter((s) => s.read && !s.revoked && !s.remote_paused);
  const resumable = sources.filter((s) => s.read && !s.revoked && s.remote_paused);
  const stale = sources.some((s) => s.report_stale);
  const reportedAt = sources.map((s) => s.report_at).sort().at(-1);

  const headers = (
    <div
      role="row"
      className={cn(
        GRID,
        'bg-surface-2 px-3 py-1.5 text-[10px] font-semibold tracking-wide text-fg-subtle uppercase',
      )}
    >
      <span role="columnheader">
        <span className="sr-only">Details</span>
      </span>
      <span role="columnheader">Source</span>
      <span role="columnheader">On the {device}</span>
      <span role="columnheader" className="max-sm:hidden">
        Server
      </span>
      <span role="columnheader">
        <span className="sr-only">Actions</span>
      </span>
    </div>
  );

  // One source: its row, and the details row under it when it is open. Callers own the rowgroup.
  const sourceRows = (source: FleetSourceStatus, indent: boolean) => {
    const isOpen = open.has(source.source_id);
    return (
      <Fragment key={source.source_id}>
        <div role="row" className={cn(GRID, 'px-3 py-1.5 text-[13px]', indent && 'border-t border-edge bg-surface-1')}>
          <span role="cell">
            <Caret open={isOpen} />
          </span>
          <span role="cell" className="min-w-0">
            <button
              type="button"
              aria-expanded={isOpen}
              onClick={() => toggle(source.source_id)}
              className={cn('flex w-full min-w-0 cursor-pointer items-center gap-1.5 text-left', indent && 'pl-3 text-fg-muted')}
            >
              <span className="truncate">{indent ? `${sourceBrowser(source)} cookies` : source.label}</span>
              {source.remote_paused ? <Badge variant="accent">paused</Badge> : null}
              {macOS && source.announcement ? <Badge variant="accent">NEW</Badge> : null}
            </button>
          </span>
          <span role="cell">
            <MacBadge status={macStatus(source)} device={device} source={source} />
          </span>
          <span role="cell" className="max-sm:hidden">
            <CollectionBadge source={source} />
          </span>
          <span role="cell">
            <SourceMenu source={source} actions={actions} />
          </span>
        </div>
        {isOpen ? (
          <div role="row">
            <div role="cell">
              <SourceDetails source={source} commands={commands} macOS={macOS} actions={actions} />
            </div>
          </div>
        ) : null}
      </Fragment>
    );
  };

  const serviceRow = (group: ServiceGroup) => {
    const key = `service:${group.key}`;
    const isOpen = open.has(key);
    return (
      <div key={key} role="rowgroup" className="border-t border-edge">
        <div role="row" className={cn(GRID, 'px-3 py-1.5 text-[13px]')}>
          <span role="cell">
            <Caret open={isOpen} />
          </span>
          <span role="cell" className="min-w-0">
            <button
              type="button"
              aria-expanded={isOpen}
              aria-label={`${group.label} browsers`}
              onClick={() => toggle(key)}
              className="flex min-w-0 cursor-pointer flex-col items-start gap-1 text-left"
            >
              <span className="max-w-full truncate">{group.label}</span>
              <span className="flex flex-wrap gap-1">
                {group.sources.map((s) => (
                  <span
                    key={s.source_id}
                    className="inline-flex items-center gap-1 rounded-full border border-edge-strong px-1.5 text-[10px] leading-4 text-fg-muted"
                  >
                    <span aria-hidden className={cn('size-1.5 rounded-full', STATUS[macStatus(s)].dot)} />
                    {sourceBrowser(s)}
                  </span>
                ))}
              </span>
            </button>
          </span>
          <span role="cell">
            <MacBadge status={rollup(group.sources)} device={device} />
          </span>
          <span role="cell" className="max-sm:hidden">
            <ServiceCollection sources={group.sources} />
          </span>
          <span role="cell" />
        </div>
        {isOpen ? group.sources.map((s) => sourceRows(s, true)) : null}
      </div>
    );
  };

  const empty = shownApps.length === 0 && shownServices.length === 0;
  return (
    <section aria-label={heading} className="flex flex-col gap-3 rounded-lg border border-edge p-4">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] font-semibold">{heading}</h3>
        {reportedAt ? (
          <Badge variant={stale ? 'warning' : 'neutral'} className="font-normal">
            {stale ? 'Stale source report' : 'Source reported'} · {timeAgo(reportedAt)}
          </Badge>
        ) : null}
      </div>
      <details className="text-xs text-fg-muted">
        <summary className="cursor-pointer text-accent-text">How this works</summary>
        <p className="mt-1.5">
          {macOS
            ? 'Read permission and renewal permission are granted locally on the Mac. Setup requests show a local notice; they never open a Keychain prompt.'
            : 'These controls govern server collection. Existing credential collection does not require macOS Keychain consent. Re-pair the device to reconnect a revoked contribution.'}{' '}
          Offline reporting does not expire eligible credentials. Read access does not prove update continuity.
        </p>
        {!macOS ? (
          <p className="mt-1.5">
            Source reporting updates when credentials are submitted. For device connectivity, check “Last seen” on the
            Fleet card. Server collection health is shown per source.
          </p>
        ) : null}
      </details>

      {query.isPending ? <p role="status" className="text-xs text-fg-muted">Loading sources…</p> : null}
      {query.isError ? (
        <p role="alert" className="text-xs text-critical">
          Source status unavailable. Local permissions remain unchanged.
        </p>
      ) : null}
      {query.data && sources.length === 0 ? (
        <p className="text-xs text-fg-muted">Waiting for the sidecar’s first source report.</p>
      ) : null}

      {sources.length > 0 ? (
        <>
          {macOS ? (
            <div className="grid grid-cols-4 gap-2 max-sm:grid-cols-2" role="group" aria-label="Filter sources">
              <Tile value={`${counts.on}/${counts.total}`} label="on" active={filter === 'on'} onClick={() => pick('on')} />
              <Tile
                value={counts.collecting}
                label="collecting"
                tone="text-ok"
                active={filter === 'collecting'}
                onClick={() => pick('collecting')}
              />
              <Tile
                value={counts.attention}
                label="need attention"
                tone={counts.attention ? 'text-critical' : undefined}
                active={filter === 'attention'}
                onClick={() => pick('attention')}
              />
              <Tile value={counts.off} label="off" active={filter === 'off'} onClick={() => pick('off')} />
            </div>
          ) : null}

          {stale ? (
            <Callout tone="warning" icon={<TriangleAlert className="size-4" />}>
              The {device} has not reported its sources recently. Statuses may be out of date; credentials the server
              already has keep working.
            </Callout>
          ) : null}
          {macOS && needAuth.length ? (
            <Callout
              icon={<Info className="size-4" />}
              action={
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy}
                  onClick={() => bulk.mutate({ targets: needAuth, action: 'request_setup' })}
                >
                  Ask Mac to set up {needAuth.length}
                </Button>
              }
            >
              {needAuth.length} enabled source{needAuth.length === 1 ? ' needs' : 's need'} authorization. On the Mac,
              open the sidecar and press <strong>Authorize all enabled</strong>, or send it a reminder.
            </Callout>
          ) : null}
          {macOS && fresh.length ? (
            <Callout
              role="status"
              icon={<Info className="size-4" />}
              action={
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => bulk.mutate({ targets: fresh, action: 'dismiss' })}
                >
                  Dismiss
                </Button>
              }
            >
              {fresh.length} new Keychain scraper{fresh.length === 1 ? '' : 's'}: off until you opt in on the Mac.
            </Callout>
          ) : null}

          <div role="table" aria-label={heading} className="overflow-hidden rounded-md border border-edge">
            {shownApps.length ? (
              <>
                {headers}
                {shownApps.map((s) => (
                  <div key={s.source_id} role="rowgroup" className="border-t border-edge">
                    {sourceRows(s, false)}
                  </div>
                ))}
              </>
            ) : null}
            {shownServices.length ? (
              <>
                <div
                  role="row"
                  className={cn(
                    GRID,
                    'border-t border-edge bg-surface-2 px-3 py-1.5 text-[10px] font-semibold tracking-wide text-fg-subtle uppercase',
                  )}
                >
                  <span role="columnheader">
                    <span className="sr-only">Details</span>
                  </span>
                  <span role="columnheader">Web sessions</span>
                  <span role="columnheader">On the {device}</span>
                  <span role="columnheader" className="max-sm:hidden">
                    Server
                  </span>
                  <span role="columnheader">
                    <span className="sr-only">Actions</span>
                  </span>
                </div>
                {shownServices.map(serviceRow)}
              </>
            ) : null}
            {empty ? (
              <div role="row">
                <p role="cell" className="px-3 py-3 text-xs text-fg-muted">
                  {filter ? 'Nothing matches this filter.' : 'No sources are on.'}
                </p>
              </div>
            ) : null}
            {!filter && counts.off > 0 ? (
              <div role="row" className="border-t border-edge">
                <div role="cell">
                  <button
                    type="button"
                    aria-expanded={showOff}
                    onClick={() => setShowOff((v) => !v)}
                    className="w-full cursor-pointer px-3 py-1.5 text-left text-xs text-fg-muted hover:bg-surface-2 hover:text-fg"
                  >
                    {showOff
                      ? 'Hide sources that are off'
                      : `Show ${counts.off} ${counts.off === 1 ? 'source that is' : 'sources that are'} off`}
                  </button>
                </div>
              </div>
            ) : null}
          </div>

          {macOS ? (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={busy || pausable.length === 0}
                onClick={() => bulk.mutate({ targets: pausable, action: 'pause' })}
              >
                Pause all
              </Button>
              <Button
                size="sm"
                variant="secondary"
                disabled={busy || resumable.length === 0}
                onClick={() => bulk.mutate({ targets: resumable, action: 'resume' })}
              >
                Resume all
              </Button>
              <span className="ml-auto text-[11px] text-fg-subtle">Revoke is per source (⋯)</span>
            </div>
          ) : null}
        </>
      ) : null}

      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(next) => {
          if (!next) setRevoking(null);
        }}
        title={revoking ? `Revoke ${revoking.label}?` : 'Revoke'}
        description={
          macOS
            ? 'The server stops using this contribution. Turn the source off and on again on the Mac to reconnect it.'
            : 'The server stops using this contribution. Revoking requires re-pairing the device to reconnect it.'
        }
        confirmLabel="Revoke contribution"
        pending={command.isPending}
        onConfirm={() => {
          if (revoking) command.mutate({ source: revoking, action: 'revoke' });
          setRevoking(null);
        }}
      />
    </section>
  );
}

export { SidecarSourceNotice } from './SidecarSourceNotice';
