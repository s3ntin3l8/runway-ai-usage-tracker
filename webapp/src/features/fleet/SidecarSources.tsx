import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { commandSidecarSource, fetchSidecarSources } from '@/api/endpoints';
import type { FleetSourceAction } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { timeAgo } from '@/lib/format';

export function SidecarSources({
  sidecarId,
  osPlatform,
}: {
  sidecarId: string;
  osPlatform?: string | null;
}) {
  const queryClient = useQueryClient();
  const macOS = osPlatform?.startsWith('Darwin') === true;
  const compatible = macOS || /^(Windows|Linux)(\/|$)/.test(osPlatform ?? '');
  const heading = macOS ? 'Keychain sources' : 'Credential sources';
  const enrolled = /^device-[a-f0-9]{32}$/.test(sidecarId);
  const queryKey = ['fleet', 'sources', sidecarId];
  const query = useQuery({
    queryKey,
    queryFn: () => fetchSidecarSources(sidecarId),
    enabled: enrolled && compatible,
    refetchInterval: 30_000,
  });
  const command = useMutation({
    mutationFn: ({
      sourceId,
      action,
    }: {
      sourceId: string;
      action: FleetSourceAction;
    }) => commandSidecarSource(sidecarId, sourceId, action),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      toast.success('Source command recorded');
    },
    onError: (error) => toast.error(error.message),
  });
  if (!compatible)
    return (
      <section aria-label={heading}>
        <h3 className="text-sm font-medium">{heading}</h3>
        <p className="text-xs text-fg-muted">
          Keychain scrapers are available on macOS. Waiting for a compatible
          device report.
        </p>
      </section>
    );
  if (!enrolled)
    return (
      <section aria-label={heading}>
        <h3 className="text-sm font-medium">{heading}</h3>
        <p className="text-xs text-fg-muted">
          Limited: pair this sidecar again to enroll its device identity.
          Existing file and environment sources keep working.
        </p>
      </section>
    );
  return (
    <section
      aria-label={heading}
      className="flex flex-col gap-3 border-t border-edge pt-4"
    >
      <h3 className="text-sm font-medium">{heading}</h3>
      <p className="text-xs text-fg-muted">
        {macOS ? (
          <>
            Read permission and renewal permission are granted locally on the Mac.
            Setup requests show a local notice; they never open a Keychain prompt.
          </>
        ) : (
          <>
            These controls govern server collection. Existing credential
            collection does not require macOS Keychain consent. Re-pair the device
            to reconnect a revoked contribution.
          </>
        )}
      </p>
      {query.isPending ? <p role="status">Loading sources…</p> : null}
      {query.isError ? (
        <p role="alert">
          Source status unavailable. Local permissions remain unchanged.
        </p>
      ) : null}
      {query.data?.sources.length === 0 ? (
        <p className="text-xs text-fg-muted">
          Waiting for the sidecar’s first source report.
        </p>
      ) : null}
      {query.data?.sources.map((source) => {
        const pending = query.data.commands.filter(
          (entry) => entry.source_id === source.source_id,
        );
        return (
          <article
            key={source.source_id}
            className="rounded-md border border-edge bg-surface-2 p-3"
          >
            <h4 className="text-sm font-medium">{source.label}</h4>
            {macOS && source.announcement ? (
              <div role="status" className="my-2 text-xs text-fg-muted">
                <p>New Keychain scraper—opt in now</p>
                <p>
                  Open Runway on this Mac → Sources → Use Keychain, then
                  Authorize.
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={command.isPending}
                  onClick={() =>
                    command.mutate({
                      sourceId: source.source_id,
                      action: 'dismiss',
                    })
                  }
                >
                  Dismiss notice for {source.label}
                </Button>
              </div>
            ) : null}
            <p className="text-xs text-fg-muted">
              Local permission: {source.read ? 'On' : 'Off'} · {source.state}
              {source.remote_paused ? ' · Paused by server' : ''}
              {source.revoked ? ' · Contribution revoked' : ''}
            </p>
            <p className="text-xs text-fg-muted">
              {macOS ? 'Last Keychain read:' : 'Last local credential read:'}{' '}
              {source.last_read_at
                ? timeAgo(new Date(source.last_read_at * 1000).toISOString())
                : 'Not reported'}
            </p>
            <p className="text-xs text-fg-muted">
              Server collection: {source.collection_health.replaceAll('_', ' ')}
              {source.last_collection_at
                ? ' · Last success ' + timeAgo(source.last_collection_at)
                : ''}
            </p>
            <p className="text-xs text-fg-muted">
              {source.report_stale ? 'Stale source report' : 'Source reported'}{' '}
              · {timeAgo(source.report_at)}. Offline reporting does not expire
              eligible credentials.
            </p>
            {!macOS ? (
              <p className="text-xs text-fg-muted">
                Source reporting updates when credentials are submitted. Device
                connectivity is shown separately from source reporting and server
                collection health.
              </p>
            ) : null}
            <p className="text-xs text-fg-muted">
              Renewal:{' '}
              {!source.renewal_capable
                ? 'Read-only scraper'
                : source.renewal_supported
                  ? source.renewal_requested
                    ? 'Enabled locally'
                    : 'Off locally'
                  : 'Limited: supported write path not verified'}
              . Read access does not prove update continuity.
            </p>
            {pending.length ? (
              <p role="status" className="text-xs text-fg-muted">
                Awaiting local acknowledgment:{' '}
                {pending
                  .map((entry) => entry.action.replaceAll('_', ' '))
                  .join(', ')}
              </p>
            ) : null}
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="secondary"
                aria-label={
                  (source.remote_paused
                    ? 'Resume server pause for '
                    : 'Pause ') + source.label
                }
                disabled={command.isPending || !source.read || source.revoked}
                onClick={() =>
                  command.mutate({
                    sourceId: source.source_id,
                    action: source.remote_paused ? 'resume' : 'pause',
                  })
                }
              >
                {source.remote_paused ? 'Resume server pause' : 'Pause source'}
              </Button>
              <Button
                size="sm"
                variant="secondary"
                aria-label={'Revoke ' + source.label + ' contribution'}
                aria-describedby={
                  !macOS
                    ? `${sidecarId}-${source.source_id}-revoke-help`
                    : undefined
                }
                disabled={command.isPending || source.revoked}
                onClick={() =>
                  command.mutate({
                    sourceId: source.source_id,
                    action: 'revoke',
                  })
                }
              >
                Revoke contribution
              </Button>
              {macOS ? (
                <Button
                  size="sm"
                  variant="secondary"
                  aria-label={'Request local setup for ' + source.label}
                  disabled={command.isPending}
                  onClick={() =>
                    command.mutate({
                      sourceId: source.source_id,
                      action: 'request_setup',
                    })
                  }
                >
                  Request local setup
                </Button>
              ) : null}
            </div>
            {!macOS ? (
              <p
                id={`${sidecarId}-${source.source_id}-revoke-help`}
                className="mt-2 text-xs text-fg-muted"
              >
                Revoking this contribution requires re-pairing the device to
                reconnect it.
              </p>
            ) : null}
          </article>
        );
      })}
    </section>
  );
}

export function SidecarSourceNotice({
  sidecarId,
  onOpen,
  osPlatform,
}: {
  sidecarId: string;
  osPlatform?: string | null;
  onOpen: () => void;
}) {
  const query = useQuery({
    queryKey: ['fleet', 'sources', sidecarId],
    queryFn: () => fetchSidecarSources(sidecarId),
    enabled:
      osPlatform?.startsWith('Darwin') === true &&
      /^device-[a-f0-9]{32}$/.test(sidecarId),
    refetchInterval: 60_000,
  });
  if (
    osPlatform?.startsWith('Darwin') !== true ||
    !query.data?.sources.some((source) => source.announcement)
  )
    return null;
  return (
    <aside
      className="mt-3 rounded-md border border-edge bg-surface-2 p-3"
      aria-label="New Keychain scraper"
    >
      <p className="text-xs font-medium">New Keychain scraper—opt in now</p>
      <p className="text-xs text-fg-muted">
        Review what this scraper reads and choose locally on your Mac.
      </p>
      <Button size="sm" variant="ghost" onClick={onOpen}>
        Review Keychain scrapers
      </Button>
    </aside>
  );
}
