import type { MergedAccount, ProviderAccount, ProviderConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { timeAgo } from '@/lib/format';
import { accountName } from './accountName';
import { PermanentDeleteButton } from './PermanentDeleteButton';
import { UnmergeButton } from './UnmergeButton';
import type { UseProvidersResult } from './useProviders';

export function MergedAccountsSection({
  mergedAccounts,
  pendingPermanentDeleteKey,
  setPendingPermanentDeleteKey,
  configs,
}: {
  mergedAccounts: {
    provider: ProviderConfig;
    merged: MergedAccount;
    leftover: ProviderAccount | undefined;
  }[];
  pendingPermanentDeleteKey: string | null;
  setPendingPermanentDeleteKey: (key: string | null) => void;
  configs: UseProvidersResult['configs'];
}) {
  // The accounts that were merged away. They are not accounts any more —
  // the merge moved their history and their credential to the destination
  // — so this list says where each of them went, and offers the one thing
  // that can still be done: putting the merge back.
  return mergedAccounts.length > 0 ? (
    <section className="mt-6 max-w-2xl" aria-label="Merged provider accounts">
      <h3 className="mb-2 text-sm font-semibold">Merged accounts ({mergedAccounts.length})</h3>
      <div className="flex flex-col gap-2">
        {mergedAccounts.map(({ provider, merged, leftover }) => (
          <Card
            key={`${provider.provider_id}/${merged.account_id}`}
            className="flex flex-wrap items-center gap-3 px-4 py-3"
          >
            <ProviderGlyph providerId={provider.provider_id} name={provider.name} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-medium">{provider.name}</p>
              <p className="truncate text-[11px] text-fg-subtle">
                {accountName(provider, merged.account_id)}
                {' → '}
                {accountName(provider, merged.destination_account_id)}
              </p>
            </div>
            <Badge variant="neutral">Merged</Badge>
            <span className="text-[11px] text-fg-subtle">{timeAgo(merged.merged_at)}</span>
            <UnmergeButton
              providerId={provider.provider_id}
              accountId={merged.account_id}
              onUnmerged={() => configs.refetch()}
            />
            {leftover?.archived && leftover.has_usage_events === false ? (
              <PermanentDeleteButton
                providerId={provider.provider_id}
                accountId={merged.account_id}
                pendingKey={pendingPermanentDeleteKey}
                setPendingKey={setPendingPermanentDeleteKey}
                onDeleted={() => configs.refetch()}
              />
            ) : null}
          </Card>
        ))}
      </div>
    </section>
  ) : null;
}
