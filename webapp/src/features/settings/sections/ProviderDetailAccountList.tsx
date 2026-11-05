// Account list for the provider detail dialog: the empty state, one row per
// visible account, and the Add account action under the list.

import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import type { ProviderAccount, ProviderConfig } from '@/api/types';
import {
  ProviderDetailAccountDetail,
  type ProviderDetailAccountDetailProps,
} from './ProviderDetailAccountDetail';

interface ProviderDetailAccountListProps
  extends Omit<ProviderDetailAccountDetailProps, 'account'> {
  listedAccounts: ProviderAccount[];
  onAddAccount?: (provider: ProviderConfig) => void;
}

export function ProviderDetailAccountList({
  provider,
  listedAccounts,
  onAddAccount,
  ...rowProps
}: ProviderDetailAccountListProps) {
  return (
    <>
      {listedAccounts.length === 0 ? (
        <EmptyState
          title="No accounts configured"
          description="Add a credential to start collecting usage for this provider."
          action={
            onAddAccount ? (
              <Button variant="primary" size="sm" onClick={() => onAddAccount(provider)}>
                Add account
              </Button>
            ) : (
              <Button variant="primary" size="sm" disabled title="Wizard disabled">
                Add account
              </Button>
            )
          }
        />
      ) : (
        <ul className="flex flex-col gap-1.5" aria-label="Provider accounts">
          {listedAccounts.map((account) => (
            <ProviderDetailAccountDetail
              key={`${account.provider_id ?? provider.provider_id}/${account.account_id}`}
              account={account}
              provider={provider}
              {...rowProps}
            />
          ))}
        </ul>
      )}

      {listedAccounts.length > 0 ? (
        onAddAccount ? (
          <Button
            variant="secondary"
            size="sm"
            className="self-start"
            onClick={() => onAddAccount(provider)}
          >
            Add account
          </Button>
        ) : (
          <Button variant="secondary" size="sm" className="self-start" disabled title="Wizard disabled">
            Add account
          </Button>
        )
      ) : null}
    </>
  );
}
