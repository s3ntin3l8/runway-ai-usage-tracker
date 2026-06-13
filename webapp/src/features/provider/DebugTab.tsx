// Debug: authoritative-source + token-health panes (always shown) plus an
// on-demand capture of raw upstream collector responses (admin-gated, runs
// live HTTP calls — never auto-fetches).

import { useState } from 'react';
import { Bug } from 'lucide-react';
import type { FleetEntry, TokenHealthEntry, TokenHealthStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatusDot } from '@/components/ui/StatusDot';
import { useTokenHealth } from '@/features/home/queries';
import { timeAgo, timeUntil } from '@/lib/format';
import type { QuotaStatus } from '@/lib/quota';
import { useDebugRaw } from './queries';

export function DebugTab({
  providerId,
  accountId,
  entry,
  active,
}: {
  providerId: string;
  accountId: string;
  entry: FleetEntry;
  active: boolean;
}) {
  const g = entry.critical_gauge;
  // Raw capture replays a server-side api/web collector. Locally-collected
  // providers (e.g. antigravity: sidecar LSP probe / local quota file) have no
  // server collector, so capture would 404 — don't offer it.
  const captureSupported = !(g.data_source === 'local' || g.input_source === 'sidecar');

  return (
    <div className="flex flex-col gap-4">
      <SourcePane entry={entry} />
      <TokenHealthPane providerId={providerId} accountId={accountId} />
      <RawCapturePane
        providerId={providerId}
        active={active}
        captureSupported={captureSupported}
      />
    </div>
  );
}

// "Authoritative source": where this provider's primary card came from, and
// the poll cadence behind it — read straight off the critical_gauge.
function SourcePane({ entry }: { entry: FleetEntry }) {
  const g = entry.critical_gauge;
  const source = [g.data_source, g.input_source].filter(Boolean).join(' · ') || '—';
  const rows: [string, string][] = [
    ['Account', g.account_label || entry.account_id],
    ['Plan', g.tier || '—'],
    ['Window', g.window_type || '—'],
    ['Kind', g.is_unlimited ? 'unlimited' : g.error_type ? 'error' : 'quota'],
    ['Source', source],
    ['Cache TTL', g.cache_ttl_seconds != null ? `${g.cache_ttl_seconds}s` : '—'],
    ['Last poll', g.fetched_at ? timeAgo(g.fetched_at) : '—'],
    ['Next poll', nextPollLabel(g.next_poll_at)],
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Authoritative source</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          {rows.map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt className="text-[11px] text-fg-subtle">{label}</dt>
              <dd className="mt-0.5 truncate text-[13px]" title={value}>
                {value}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

function nextPollLabel(iso: string | null | undefined): string {
  const until = timeUntil(iso);
  if (!until) return '—';
  return until === 'now' ? 'now' : `in ${until}`;
}

// "Token health": OAuth / API-key expiry for this account. Admin-gated — the
// query is retry:false and may 403 on a locked-down remote, in which case we
// render nothing rather than an error.
function TokenHealthPane({ providerId, accountId }: { providerId: string; accountId: string }) {
  const health = useTokenHealth();
  const entries = (health.data?.tokens ?? []).filter(
    (t) => t.provider === providerId && t.account_id === accountId,
  );

  if (health.isError || entries.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Token health</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {entries.map((t, i) => (
          <TokenHealthRow key={`${t.account_id}-${i}`} token={t} />
        ))}
      </CardContent>
    </Card>
  );
}

function TokenHealthRow({ token }: { token: TokenHealthEntry }) {
  const expiry =
    token.status === 'expired'
      ? 'expired'
      : token.expires_at
        ? `expires in ${timeUntil(token.expires_at) ?? '—'}`
        : 'no expiry';

  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2">
        <StatusDot status={tokenStatus(token.status)} label={token.status} />
        <div className="min-w-0">
          <p className="text-[13px]">
            {(token.token_types ?? []).join(', ') || 'token'}
            {token.can_refresh ? (
              <Badge variant="neutral" className="ml-2">
                auto-rotate
              </Badge>
            ) : null}
          </p>
          {token.source ? (
            <p className="truncate text-[11px] text-fg-subtle">{token.source}</p>
          ) : null}
        </div>
      </div>
      <span className="shrink-0 text-[12px] text-fg-muted">{expiry}</span>
    </div>
  );
}

function tokenStatus(status: TokenHealthStatus): QuotaStatus {
  if (status === 'valid') return 'ok';
  if (status === 'expiring') return 'warning';
  if (status === 'expired') return 'critical';
  return 'unknown';
}

function RawCapturePane({
  providerId,
  active,
  captureSupported,
}: {
  providerId: string;
  active: boolean;
  captureSupported: boolean;
}) {
  const [requested, setRequested] = useState(false);
  const debug = useDebugRaw(providerId, captureSupported && active && requested);

  if (!captureSupported) {
    return (
      <EmptyState
        icon={Bug}
        title="Raw capture unavailable"
        description={`${providerId} is collected locally by the sidecar (LSP probe / local quota file), so there's no server-side HTTP exchange to capture. The sidecar is the source of truth — see Authoritative source above.`}
      />
    );
  }

  if (!requested) {
    return (
      <EmptyState
        icon={Bug}
        title="Capture raw collector output"
        description="Runs this provider's collector once and records the upstream HTTP exchanges (auth headers masked). Admin only; rate-limited."
        action={
          <Button variant="primary" size="sm" onClick={() => setRequested(true)}>
            Run capture
          </Button>
        }
      />
    );
  }

  if (debug.isPending) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-6 w-1/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (debug.isError) {
    return (
      <EmptyState
        icon={Bug}
        title="Capture failed"
        description={debug.error.message}
        action={
          <Button size="sm" onClick={() => debug.refetch()}>
            Retry
          </Button>
        }
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Raw collector exchange</CardTitle>
        <Button size="sm" variant="ghost" onClick={() => debug.refetch()} loading={debug.isFetching}>
          Re-run
        </Button>
      </CardHeader>
      <CardContent>
        <pre className="max-h-[32rem] overflow-auto rounded-sm bg-surface-2 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
          {JSON.stringify(debug.data, null, 2)}
        </pre>
      </CardContent>
    </Card>
  );
}
