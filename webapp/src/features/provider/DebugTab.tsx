// Debug: the collection-context pane (always shown, from the credential inventory) plus an
// on-demand capture of raw upstream collector responses (admin-gated, runs
// live HTTP calls — never auto-fetches).

import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Bug, ChevronDown, ChevronRight } from 'lucide-react';
import { Link } from 'react-router';
import { probeCredentialSources } from '@/api/endpoints';
import type {
  CredentialAccountView,
  CredentialSourceView,
  DebugRawResponse,
  FleetEntry,
  SourceProbeResult,
  StrategyCapture,
  StrategyCaptureResponse,
} from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { useCredentialInventory } from '@/features/settings/sections/credentials/queries';
import { timeAgo, timeUntil } from '@/lib/format';
import { useDebugRaw } from './queries';
import { labelOrMaskedId } from '@/lib/accountDisplay';
import { PROBE_LABEL, PROBE_VARIANT } from '@/lib/probeOutcome';
import { STATUS_LABEL, STATUS_VARIANT, originSummary } from '@/features/settings/sections/credentials/display';

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
  // Local event cards can be synthesized for registered quota providers when
  // their server collector has not returned a quota card yet. Use capability
  // from the fleet registry when available; fall back for older API responses.
  const captureSupported = entry.server_collector_available ?? g.data_source !== 'local';

  return (
    <div className="flex flex-col gap-4">
      <CollectionContextPane providerId={providerId} accountId={accountId} entry={entry} />
      <CredentialSourcesPane
        key={`${providerId}:${accountId}`}
        providerId={providerId}
        accountId={accountId}
      />
      <RawCapturePane
        providerId={providerId}
        accountId={accountId}
        active={active}
        captureSupported={captureSupported}
      />
    </div>
  );
}

function CollectionContextPane({
  providerId,
  accountId,
  entry,
}: {
  providerId: string;
  accountId: string;
  entry: FleetEntry;
}) {
  const inventory = useCredentialInventory();
  const g = entry.critical_gauge;
  const matchingAccounts = (inventory.data?.providers ?? [])
    .filter((provider) => provider.provider_id === providerId)
    .flatMap((provider) => provider.accounts)
    .filter((account) => matchesAccount(account.account_id, accountId));
  const activeSources = matchingAccounts.flatMap((account) => {
    const source = account.sources.find((item) => item.source_id === account.active_source_id);
    return source ? [{ account, source }] : [];
  });
  const cardPath = [g.data_source, g.input_source].filter(Boolean).join(' · ') || '—';
  const cardKind = g.is_unlimited ? 'unlimited' : g.error_type ? 'error' : 'quota';

  return (
    <Card>
      <CardHeader>
        <CardTitle>Collection context</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 sm:grid-cols-3">
        <section>
          <h3 className="text-[11px] font-medium text-fg-subtle">Most recently successful credential</h3>
          {inventory.isPending ? (
            <p className="mt-1 text-[13px] text-fg-muted">Loading credential details…</p>
          ) : inventory.isError ? (
            <p className="mt-1 text-[13px] text-fg-muted">Credential details unavailable.</p>
          ) : activeSources.length === 0 ? (
            <p className="mt-1 text-[13px] text-fg-muted">No successful credential source recorded.</p>
          ) : (
            activeSources.map(({ account, source }) => (
              <CredentialSourceSummary
                key={`${account.account_id}/${source.source_id}`}
                account={account}
                source={source}
              />
            ))
          )}
          <Link to="/settings/credentials" className="mt-2 inline-block text-[11px] text-accent hover:underline">
            Full credential inventory
          </Link>
        </section>
        <section>
          <h3 className="text-[11px] font-medium text-fg-subtle">Most restrictive quota card</h3>
          <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-[12px]">
            <dt className="text-fg-subtle">Plan</dt><dd className="truncate">{g.tier || '—'}</dd>
            <dt className="text-fg-subtle">Window</dt><dd className="truncate">{g.window_type || '—'}</dd>
            <dt className="text-fg-subtle">Kind</dt><dd className="truncate">{cardKind}</dd>
            <dt className="text-fg-subtle">Card path</dt><dd className="truncate" title={cardPath}>{cardPath}</dd>
          </dl>
        </section>
        <section>
          <h3 className="text-[11px] font-medium text-fg-subtle">Collector schedule · account</h3>
          <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-[12px]">
            <dt className="text-fg-subtle">Cache TTL</dt><dd>{g.cache_ttl_seconds != null ? `${g.cache_ttl_seconds}s` : '—'}</dd>
            <dt className="text-fg-subtle">Last poll</dt><dd>{g.fetched_at ? timeAgo(g.fetched_at) : '—'}</dd>
            <dt className="text-fg-subtle">Next poll</dt><dd>{nextPollLabel(g.next_poll_at)}</dd>
          </dl>
        </section>
      </CardContent>
    </Card>
  );
}

function CredentialSourceSummary({
  account,
  source,
}: {
  account: CredentialAccountView;
  source: CredentialSourceView;
}) {
  return (
    <div className="mt-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[13px]">{originSummary(source)}</span>
        <Badge variant={STATUS_VARIANT[source.status] ?? 'neutral'}>
          {STATUS_LABEL[source.status] ?? source.status}
        </Badge>
      </div>
      <p className="text-[11px] text-fg-subtle">
        {labelOrMaskedId({ account_id: account.account_id, account_label: account.account_label })}
        {source.last_success_at ? ` · collected ${timeAgo(source.last_success_at)}` : ''}
      </p>
    </div>
  );
}

// Every credential source that could feed this account, side by side, with an on-demand
// live test of each (admin-gated, rate-limited; never auto-runs). The list itself comes
// from the credential inventory; the probe changes nothing on the server.
function CredentialSourcesPane({
  providerId,
  accountId,
}: {
  providerId: string;
  accountId: string;
}) {
  const inventory = useCredentialInventory();
  const probe = useMutation({
    mutationFn: () => probeCredentialSources(providerId, accountId),
  });
  const accounts = (inventory.data?.providers ?? [])
    .filter((provider) => provider.provider_id === providerId)
    .flatMap((provider) => provider.accounts)
    .filter((account) => matchesAccount(account.account_id, accountId));
  const sources = accounts.flatMap((account) => account.sources);
  const results = new Map<string, SourceProbeResult>(
    (probe.data?.sources ?? []).map((result) => [result.source_id, result]),
  );
  // The default account's probe also reaches sources filed under identities of their own;
  // they have no row above, so list them rather than drop them.
  const listed = new Set(sources.map((source) => source.source_id));
  const extraResults = (probe.data?.sources ?? []).filter((result) => !listed.has(result.source_id));

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>Credential sources</CardTitle>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => probe.mutate()}
          disabled={probe.isPending || sources.length === 0}
        >
          {probe.isPending ? 'Probing…' : 'Probe sources'}
        </Button>
      </CardHeader>
      <CardContent>
        {inventory.isPending ? (
          <Skeleton className="h-12 w-full" />
        ) : sources.length === 0 ? (
          <p className="text-[13px] text-fg-muted">No credential source reported for this account.</p>
        ) : (
          <ul className="divide-y divide-edge" aria-label="Credential sources">
            {sources.map((source) => {
              const result = results.get(source.source_id);
              return (
                <li key={`${source.account_id}/${source.source_id}`} className="py-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[13px]">{originSummary(source)}</span>
                    <Badge variant={STATUS_VARIANT[source.status] ?? 'neutral'}>
                      {STATUS_LABEL[source.status] ?? source.status}
                    </Badge>
                    {source.is_active ? <Badge variant="accent">Feeding data</Badge> : null}
                    {!source.enabled ? <Badge variant="neutral">Disabled</Badge> : null}
                  </div>
                  <p className="text-[11px] text-fg-subtle">
                    priority {source.priority}
                    {source.last_success_at
                      ? ` · collected ${timeAgo(source.last_success_at)}`
                      : source.health === 'untried'
                        ? ' · not yet tried'
                        : ''}
                    {source.last_seen ? ` · seen ${timeAgo(source.last_seen)}` : ''}
                  </p>
                  {probe.data && !result ? (
                    <p className="mt-0.5 text-[11px] text-fg-subtle">
                      Not probed — it belongs to a different account than the one probed here.
                    </p>
                  ) : null}
                  {result ? (
                    <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px]">
                      <Badge variant={PROBE_VARIANT[result.outcome] ?? 'neutral'}>
                        {PROBE_LABEL[result.outcome] ?? result.outcome}
                      </Badge>
                      {result.http_status ? (
                        <span className="text-fg-subtle">HTTP {result.http_status}</span>
                      ) : null}
                      {result.error_type ? (
                        <span className="text-fg-subtle">{result.error_type}</span>
                      ) : null}
                      {result.probed && result.duration_ms != null ? (
                        <span className="text-fg-subtle">{result.duration_ms} ms</span>
                      ) : null}
                      {result.message ? (
                        <span className="text-critical">{result.message}</span>
                      ) : null}
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
        {extraResults.length > 0 ? (
          <ul className="mt-2 space-y-1" aria-label="Other probed sources">
            {extraResults.map((result) => (
              <li key={result.source_id} className="flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="font-mono text-fg-muted">{result.source_id}</span>
                <Badge variant={PROBE_VARIANT[result.outcome] ?? 'neutral'}>
                  {PROBE_LABEL[result.outcome] ?? result.outcome}
                </Badge>
                {result.http_status ? (
                  <span className="text-fg-subtle">HTTP {result.http_status}</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
        {probe.isError ? (
          <p className="mt-2 text-[12px] text-critical" role="alert">
            Probe failed: {probe.error.message}
          </p>
        ) : null}
        {probe.data ? (
          <p className="mt-2 text-[11px] text-fg-subtle">
            Probed {timeAgo(probe.data.probed_at)}. A probe makes one real request per source and
            changes nothing: no refresh, no health update.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function nextPollLabel(iso: string | null | undefined): string {
  const until = timeUntil(iso);
  if (!until) return '—';
  return until === 'now' ? 'now' : `in ${until}`;
}

// Match credential inventory accounts to this provider detail account. The ``default`` account
// holds the credentials not tied to one account (the server's env var, an unscoped pasted key), so
// it shows on every pane of the provider; any other account matches only its own pane.
const matchesAccount = (id: string, accountId: string) => id === accountId || id === 'default';

function RawCapturePane({
  providerId,
  accountId,
  active,
  captureSupported,
}: {
  providerId: string;
  accountId: string;
  active: boolean;
  captureSupported: boolean;
}) {
  const [requested, setRequested] = useState(false);
  const debug = useDebugRaw(providerId, accountId, captureSupported && active && requested);

  if (!captureSupported) {
    return (
      <EmptyState
        icon={Bug}
        title="Raw capture unavailable"
        description={`${providerId} has no server quota collector, so there is no server-side HTTP exchange to capture.`}
      />
    );
  }

  if (!requested) {
    return (
      <EmptyState
        icon={Bug}
        title="Capture raw collector output"
        description="Runs this provider's collector once and records the upstream HTTP exchanges (secrets redacted). Admin only; rate-limited."
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
          {capture.cards_summary.length > 0 && (
            <Section title={`Collector result (${capture.cards_summary.length})`} defaultOpen>
              {capture.cards_summary.map((card, i) => (
                <div key={i} className="mb-1 text-[12px]">
                  <strong>{card.service_name || 'Card'}:</strong>{' '}
                  {card.detail || card.remaining || 'returned'}
                  {card.error_type ? <span className="ml-1 text-critical">({card.error_type})</span> : null}
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
