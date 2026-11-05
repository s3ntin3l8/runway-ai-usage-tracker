import type { CredentialProviderView } from '@/api/types';

/** `{account_id: status}` for a provider's accounts whose credential is expired or rejected. */
export const CREDENTIAL_PROBLEMS: ReadonlySet<string> = new Set(['invalid', 'expired', 'failing']);

export function accountCredentialProblems(
  provider: CredentialProviderView | undefined,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const account of provider?.accounts ?? []) {
    if (CREDENTIAL_PROBLEMS.has(account.status)) {
      out[account.account_id] = account.status;
    }
  }
  return out;
}
