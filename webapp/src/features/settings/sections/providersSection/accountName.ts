import type { ProviderConfig } from '@/api/types';
import { labelOrMaskedId, maskAccountId } from '@/lib/accountDisplay';

/**
 * The display name of one of a provider's accounts: its label when it has
 * one, otherwise the masked id. A merged-away account is no longer listed
 * under the provider, so this falls back to the id.
 */
export function accountName(provider: ProviderConfig, accountId: string): string {
  const account = provider.accounts.find((a) => a.account_id === accountId);
  return account ? labelOrMaskedId(account) : maskAccountId(accountId);
}
