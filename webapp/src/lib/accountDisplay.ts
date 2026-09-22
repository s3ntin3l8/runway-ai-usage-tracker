// Account display helpers for the multi-account provider settings UI (#286).
// Used by ProviderDetailDialog, ProviderAccountDialog header, and the wizard's
// step-3 confirm screen (in #287).

interface AccountLike {
  account_id: string;
  account_label?: string | null;
}

/** Human-readable name for a provider account row. */
export function displayAccountName(account: AccountLike): string {
  const label = (account.account_label ?? '').trim();
  if (label !== '') return label;
  if (account.account_id === 'default') return 'Default account';
  return account.account_id;
}

/**
 * Subtitle (account_id) shown below the display name when the label is
 * distinct from the identity the display name already shows. Returns null
 * when the subtitle would duplicate the title (label === account_id, or no
 * label AND displayAccountName falls through to account_id).
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
  // No label: subtitle is omitted whenever the display name already shows
  // the account_id verbatim, regardless of whether the id looks like an
  // email/uuid/hash — the regex branches are dead code (see Hermes review
  // on PR #294: proven by mutation). The display-name fallback path
  // catches every case the early return below doesn't.
  if (displayAccountName(account) === account.account_id) return null;
  return null;
}
