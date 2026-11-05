import { toast } from 'sonner';
import { putProviderConfig } from '@/api/endpoints';
import type { ProviderAccount, ProviderConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { labelOrMaskedId } from '@/lib/accountDisplay';
import { errorMessage } from '@/lib/errorMessage';
import { PermanentDeleteButton } from './PermanentDeleteButton';
import type { UseProvidersResult } from './useProviders';

export function ArchivedAccountsSection({
  archivedOnly,
  pendingPermanentDeleteKey,
  setPendingPermanentDeleteKey,
  configs,
}: {
  archivedOnly: { provider: ProviderConfig; account: ProviderAccount }[];
  pendingPermanentDeleteKey: string | null;
  setPendingPermanentDeleteKey: (key: string | null) => void;
  configs: UseProvidersResult['configs'];
}) {
  return archivedOnly.length > 0 ? (
    <section className="mt-6 max-w-2xl" aria-label="Archived provider accounts">
      <h3 className="mb-2 text-sm font-semibold">Archived accounts ({archivedOnly.length})</h3>
      <div className="flex flex-col gap-2">
        {archivedOnly.map(({ provider, account }) => (
          <Card key={`${provider.provider_id}/${account.account_id}`} className="flex flex-wrap items-center gap-3 px-4 py-3">
            <ProviderGlyph providerId={provider.provider_id} name={provider.name} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-medium">{provider.name}</p>
              <p className="truncate text-[11px] text-fg-subtle">{labelOrMaskedId(account)}</p>
            </div>
            <Badge variant="neutral">Archived</Badge>
            <Button
              variant="secondary"
              size="sm"
              onClick={async () => {
                try {
                  await putProviderConfig(provider.provider_id, account.account_id, { archived: false });
                  await configs.refetch();
                } catch (error) {
                  toast.error(errorMessage(error, 'Could not restore account'));
                }
              }}
            >
              Restore
            </Button>
            {account.has_usage_events === false ? (
              <PermanentDeleteButton
                providerId={provider.provider_id}
                accountId={account.account_id}
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
