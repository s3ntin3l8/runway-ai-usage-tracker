// Which credentials deserve a dashboard banner, derived from the credential inventory.
// The rule is the one Token Health used: expired, expiring or rejected, and not redundant
// (an unrefreshable dead credential that another healthy one for the account can replace).

import type { CredentialAccountView, CredentialInventory, CredentialSourceView } from '@/api/types';
import { maskAccountId } from '@/lib/accountDisplay';

export type AttentionStatus = 'expired' | 'expiring' | 'invalid' | 'failing';

export interface AttentionCredential {
  provider: string;
  /** Human name of the account the credential belongs to. */
  accountName: string;
  status: AttentionStatus;
}

const ATTENTION: ReadonlySet<string> = new Set<AttentionStatus>([
  'expired',
  'expiring',
  'invalid',
  'failing',
]);

function accountName(account: CredentialAccountView, source: CredentialSourceView): string {
  const label = (account.account_label ?? '').trim();
  if (label !== '') return label;
  if (source.origin_kind === 'server') return 'Server environment';
  if (account.account_id === 'default') return 'Default account';
  return maskAccountId(account.account_id);
}

export function credentialsNeedingAttention(
  inventory: CredentialInventory | undefined,
): AttentionCredential[] {
  const out: AttentionCredential[] = [];
  for (const provider of inventory?.providers ?? []) {
    for (const account of provider.accounts) {
      for (const source of account.sources) {
        // A disabled source or an env var nothing uses can't break collection.
        if (!source.enabled || source.unused_reason || source.redundant) continue;
        if (!ATTENTION.has(source.status)) continue;
        out.push({
          provider: provider.provider_id,
          accountName: accountName(account, source),
          status: source.status as AttentionStatus,
        });
      }
    }
  }
  return out;
}
