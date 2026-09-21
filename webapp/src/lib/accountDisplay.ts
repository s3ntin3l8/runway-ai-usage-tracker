// Account display helpers for the multi-account provider settings UI (#286).
// Used by ProviderDetailDialog, ProviderAccountDialog header, and the wizard's
// step-3 confirm screen (in #287).

interface AccountLike {
  account_id: string;
  account_label?: string | null;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const HASH_RE = /^[a-f0-9]{32,}$/i;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Human-readable name for a provider account row. */
export function displayAccountName(account: AccountLike): string {
  const label = (account.account_label ?? '').trim();
  if (label !== '') return label;
  if (account.account_id === 'default') return 'Default account';
  return account.account_id;
}

/**
 * Subtitle (account_id) shown below the display name when the label is
 * distinct from the account_id (otherwise the label already covers the
 * identity and a subtitle would be a duplicate). Returns null when no
 * label is set AND the account_id doesn't look meaningful (e.g. "default"
 * or an opaque identifier).
 */
export function accountSubtitle(account: AccountLike): string | null {
  if (!account.account_id || account.account_id === 'default') return null;
  const label = (account.account_label ?? '').trim();
  if (label !== '' && label !== account.account_id) {
    return account.account_id;
  }
  if (label === account.account_id) {
    // Label already covers the identity; a subtitle would be a duplicate.
    return null;
  }
  if (
    EMAIL_RE.test(account.account_id) ||
    UUID_RE.test(account.account_id) ||
    HASH_RE.test(account.account_id)
  ) {
    return account.account_id;
  }
  return null;
}
