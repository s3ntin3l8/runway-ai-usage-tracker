import { Link } from 'react-router';
import type { FleetEntry } from '@/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { useCredentialInventory } from '@/features/settings/sections/credentials/queries';
import { timeAgo } from '@/lib/format';
import { CredentialSourceSummary } from './CredentialSourceSummary';
import { matchesAccount } from './matchesAccount';
import { nextPollLabel } from './nextPollLabel';

export function CollectionContextPane({
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
          <Link to="/settings/credentials" className="mt-2 inline-block text-[11px] text-accent-text hover:underline">
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
