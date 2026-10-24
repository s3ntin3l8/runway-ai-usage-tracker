// The accounts that were merged away — the fleet shows the merge as a note
// beside its entries.
//
// A merged-away account holds no credential and has no quota card (the merge
// moved both to the destination), so it is not a fleet entry: giving it one
// would mean inventing a gauge, and the risk rail and the stale-card banner
// would read that invented card. The operator watching usage learns about the
// merge here instead — "alice was merged into bob" — and the row leads to the
// destination, which is where the usage now is.

import { useNavigate } from 'react-router';
import { useFleet, useProviderConfigs } from '@/features/home/queries';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { labelOrMaskedId, maskAccountId } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';
import type { ProviderConfig } from '@/api/types';

/** The display name of one of a provider's accounts: its label when it has
 *  one, otherwise the masked id. */
function accountName(provider: ProviderConfig | undefined, accountId: string): string {
  const account = provider?.accounts.find((a) => a.account_id === accountId);
  return account ? labelOrMaskedId(account) : maskAccountId(accountId);
}

export function MergedSection() {
  const fleet = useFleet();
  const configs = useProviderConfigs();
  const navigate = useNavigate();

  const merged = fleet.data?.merged_accounts ?? [];
  if (merged.length === 0) return null;

  const providers = new Map(
    (configs.data?.providers ?? []).map((p) => [p.provider_id, p]),
  );

  return (
    <section aria-label="Merged accounts" className="flex flex-col gap-2">
      <h2 className="text-xs font-semibold tracking-wide text-fg-subtle uppercase">Merged</h2>
      <div className="flex flex-col gap-1.5">
        {merged.map((m) => {
          const provider = providers.get(m.provider_id);
          const providerName = provider?.name ?? m.provider_id;
          return (
            <button
              key={`${m.provider_id}:${m.account_id}`}
              type="button"
              onClick={() =>
                navigate(
                  `/provider/${m.provider_id}?account=${encodeURIComponent(m.destination_account_id)}`,
                )
              }
              className="flex items-center gap-2.5 rounded border border-border bg-surface-1 px-3 py-2 text-left transition-colors hover:border-edge-strong"
            >
              <ProviderGlyph
                providerId={m.provider_id}
                name={providerName}
                className="size-5 text-[9px]"
              />
              <span className="min-w-0 flex-1 truncate text-[12px]">
                <span className="font-medium">{providerName}</span>
                <span className="text-fg-subtle"> · </span>
                <span>{accountName(provider, m.account_id)}</span>
                <span className="text-fg-subtle"> → </span>
                <span>{accountName(provider, m.destination_account_id)}</span>
              </span>
              <span className="shrink-0 text-[11px] text-fg-subtle">{timeAgo(m.merged_at)}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
