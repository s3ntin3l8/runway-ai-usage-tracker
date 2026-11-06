import { useMutation } from '@tanstack/react-query';
import { probeCredentialSources } from '@/api/endpoints';
import type { SourceProbeResult } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { STATUS_LABEL, STATUS_VARIANT, originSummary } from '@/features/settings/sections/credentials/display';
import { useCredentialInventory } from '@/features/settings/sections/credentials/queries';
import { timeAgo } from '@/lib/format';
import { PROBE_LABEL, PROBE_VARIANT } from '@/lib/probeOutcome';
import { matchesAccount } from './matchesAccount';

// Every credential source that could feed this account, side by side, with an on-demand
// live test of each (admin-gated, rate-limited; never auto-runs). The list itself comes
// from the credential inventory; the probe changes nothing on the server.
export function CredentialSourcesPane({
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
