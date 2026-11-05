// Per-source actions menu and the expanded detail row. The parent records the commands.

import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Ellipsis } from 'lucide-react';
import type { FleetSourceAction, FleetSourceCommand, FleetSourceStatus } from '@/api/types';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';

export interface Actions {
  busy: boolean;
  macOS: boolean;
  run: (source: FleetSourceStatus, action: FleetSourceAction) => void;
  revoke: (source: FleetSourceStatus) => void;
}

export function SourceMenu({ source, actions }: { source: FleetSourceStatus; actions: Actions }) {
  const item =
    'flex cursor-pointer items-center rounded-md px-2 py-1.5 text-[12px] outline-none data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50 data-[highlighted]:bg-surface-2';
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={`Actions for ${source.label}`}
          className="flex size-6 cursor-pointer items-center justify-center rounded-sm text-fg-subtle transition-colors hover:bg-surface-3 hover:text-fg"
        >
          <Ellipsis className="size-4" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="z-[60] min-w-48 rounded-lg border border-edge bg-overlay p-1 shadow-md"
        >
          <DropdownMenu.Item
            className={item}
            disabled={actions.busy || !source.read || source.revoked}
            onSelect={() => actions.run(source, source.remote_paused ? 'resume' : 'pause')}
          >
            {source.remote_paused ? 'Resume server pause' : 'Pause collection'}
          </DropdownMenu.Item>
          {actions.macOS ? (
            <DropdownMenu.Item
              className={item}
              disabled={actions.busy}
              onSelect={() => actions.run(source, 'request_setup')}
            >
              Ask Mac to set up
            </DropdownMenu.Item>
          ) : null}
          <DropdownMenu.Item
            className={cn(item, 'text-critical')}
            disabled={actions.busy || source.revoked}
            onSelect={() => actions.revoke(source)}
          >
            Revoke contribution…
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

export function SourceDetails({
  source,
  commands,
  macOS,
  actions,
}: {
  source: FleetSourceStatus;
  commands: FleetSourceCommand[];
  macOS: boolean;
  actions: Actions;
}) {
  const pending = commands.filter((c) => c.source_id === source.source_id);
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 border-t border-edge bg-surface-2 py-2 pr-3 pl-9 text-[11px]">
      <dt className="text-fg-subtle">{macOS ? 'Last Keychain read' : 'Last local credential read'}</dt>
      <dd>
        {source.last_read_at ? timeAgo(new Date(source.last_read_at * 1000).toISOString()) : 'Not reported'}
      </dd>
      <dt className="text-fg-subtle">Server collection</dt>
      <dd>
        {source.collection_health.replaceAll('_', ' ')}
        {source.last_collection_at ? ` · last success ${timeAgo(source.last_collection_at)}` : ''}
        {source.remote_paused ? ' · paused by server' : ''}
      </dd>
      <dt className="text-fg-subtle">Report</dt>
      <dd>
        {source.report_stale ? 'Stale source report' : 'Source reported'} · {timeAgo(source.report_at)}
      </dd>
      {source.renewal_capable ? (
        <>
          <dt className="text-fg-subtle">Renewal</dt>
          <dd>
            {source.renewal_supported
              ? source.renewal_requested
                ? 'Enabled on the Mac'
                : 'Off on the Mac'
              : 'Limited: supported write path not verified'}
          </dd>
        </>
      ) : null}
      {pending.length ? (
        <>
          <dt className="text-fg-subtle">Pending</dt>
          <dd>
            <span role="status">
              Awaiting local acknowledgment: {pending.map((c) => c.action.replaceAll('_', ' ')).join(', ')}
            </span>
          </dd>
        </>
      ) : null}
      {macOS && source.announcement ? (
        <>
          <dt className="text-fg-subtle">New scraper</dt>
          <dd>
            <button
              type="button"
              className="font-medium text-accent-text hover:underline disabled:opacity-50"
              disabled={actions.busy}
              onClick={() => actions.run(source, 'dismiss')}
            >
              Dismiss notice for {source.label}
            </button>
          </dd>
        </>
      ) : null}
    </dl>
  );
}
