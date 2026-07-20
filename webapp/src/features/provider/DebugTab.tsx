// Debug: authoritative-source + token-health panes (always shown) plus an
// on-demand capture of raw upstream collector responses (admin-gated, runs
// live HTTP calls — never auto-fetches).

import { useState } from 'react';
import { Bug, ChevronDown, ChevronRight } from 'lucide-react';
import type {
  DebugRawResponse,
  FleetEntry,
  StrategyCapture,
  StrategyCaptureResponse,
  TokenHealthEntry,
  TokenHealthStatus,
} from '@/api/types';
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
  // Raw capture replays a server-side api/web collector. Providers whose
  // critical gauge has data_source='local' are sidecar-only (enrichment-only
  // providers like OpenCode events) and have no server collector.
  // input_source='sidecar' only means credentials came from a remote agent;
  // the server still makes the HTTP calls, so capture is supported.
  const captureSupported = g.data_source !== 'local';

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

// Generic account_id values used by UI-configured or local-file credentials.
// These will never equal a user's real account_id, so we always include them
// when the provider matches — they are this provider's credentials too.
const GENERIC_ACCOUNT_IDS = new Set(['config', 'config-cookie', 'local-file']);

// "Token health": OAuth / API-key expiry for this account. Admin-gated — the
// query is retry:false and may 403 on a locked-down remote, in which case we
// render nothing rather than an error.
function TokenHealthPane({ providerId, accountId }: { providerId: string; accountId: string }) {
  const health = useTokenHealth();
  const entries = (health.data?.tokens ?? []).filter(
    (t) =>
      t.provider === providerId &&
      (t.account_id === accountId || GENERIC_ACCOUNT_IDS.has(t.account_id)),
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
        description={`${providerId} is enrichment-only (sidecar-side event extraction) — there is no server-side HTTP exchange to capture.`}
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

  const data = debug.data as DebugRawResponse;
  const strategyIds = Object.keys(data.strategies ?? {});

  return (
    <Card>
      <CardHeader>
        <CardTitle>Raw collector exchange</CardTitle>
        <Button size="sm" variant="ghost" onClick={() => debug.refetch()} loading={debug.isFetching}>
          Re-run
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {strategyIds.length === 0 ? (
          <div className="text-[12px] text-fg-muted">
            No per-strategy breakdown available (legacy collector or no strategies declared).
          </div>
        ) : (
          strategyIds.map((sId) => {
            const cap = data.strategies[sId];
            return (
              <StrategySection
                key={sId}
                strategyId={sId}
                capture={cap}
                isActive={sId === data.active_strategy}
              />
            );
          })
        )}
      </CardContent>
    </Card>
  );
}

function StrategySection({
  strategyId,
  capture,
  isActive,
}: {
  strategyId: string;
  capture: StrategyCapture;
  isActive: boolean;
}) {
  const [open, setOpen] = useState(false);

  const statusBadge: { variant: 'ok' | 'critical' | 'neutral'; label: string } =
    capture.status === 'success'
      ? { variant: 'ok', label: 'success' }
      : capture.errors.length > 0
        ? { variant: 'critical', label: capture.errors[0].type }
        : { variant: 'neutral', label: capture.status };

  return (
    <div className="rounded-sm border border-border">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] hover:bg-surface-2"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="font-medium">{capture.label}</span>
        <code className="text-[11px] text-fg-muted">{strategyId}</code>
        <Badge variant={capture.kind === 'primary' ? 'accent' : 'ok'}>{capture.kind}</Badge>
        <Badge variant={statusBadge.variant}>{statusBadge.label}</Badge>
        {capture.cards_returned > 0 && (
          <span className="text-[11px] text-fg-muted">{capture.cards_returned} cards</span>
        )}
        {isActive && (
          <Badge variant="warning" className="ml-auto">
            Active
          </Badge>
        )}
      </button>
      {open && (
        <div className="border-t border-border px-3 py-2">
          {capture.errors.length > 0 && (
            <Section title="Errors" defaultOpen>
              {capture.errors.map((e, i) => (
                <div key={i} className="mb-1 text-[12px] text-critical">
                  <strong>{e.type}:</strong> {e.message}
                </div>
              ))}
            </Section>
          )}
          {capture.requests.length > 0 && (
            <Section title={`Requests (${capture.requests.length})`}>
              {capture.requests.map((r, i) => (
                <div key={i} className="mb-1 text-[12px]">
                  <Badge variant="neutral" className="mr-1">
                    {r.method}
                  </Badge>
                  <span className="break-all font-mono text-fg-muted">{r.url}</span>
                </div>
              ))}
            </Section>
          )}
          {capture.responses.length > 0 && (
            <Section title={`Responses (${capture.responses.length})`}>
              {capture.responses.map((r, i) => (
                <ResponseBlock key={i} response={r} />
              ))}
            </Section>
          )}
          {capture.requests.length === 0 &&
            capture.responses.length === 0 &&
            capture.errors.length === 0 && (
              <div className="text-[12px] text-fg-muted">
                No HTTP traffic captured for this strategy.
              </div>
            )}
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  children,
  defaultOpen,
}: {
  title: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen ?? false);
  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 py-1 text-[12px] font-medium text-fg-muted hover:text-fg"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {title}
      </button>
      {open && <div className="ml-4">{children}</div>}
    </div>
  );
}

function ResponseBlock({ response }: { response: StrategyCaptureResponse }) {
  const [expanded, setExpanded] = useState(false);
  const statusColor =
    response.status < 300 ? 'text-success' : response.status < 500 ? 'text-warning' : 'text-critical';

  return (
    <div className="mb-2 border-l-2 border-border pl-2">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-[12px]"
      >
        <Badge variant="neutral">{response.method}</Badge>
        <span className={statusColor}>{response.status}</span>
        <span className="truncate font-mono text-fg-muted">{response.url}</span>
      </button>
      {expanded && (
        <pre className="mt-1 max-h-48 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-[11px] whitespace-pre-wrap">
          {JSON.stringify(response.body, null, 2)}
        </pre>
      )}
    </div>
  );
}
