// Account display helpers for the multi-account provider settings UI (#286).
// Used by ProviderDetailDialog, ProviderAccountDialog header, and the wizard's
// step-3 confirm screen (in #287).

interface AccountLike {
  account_id: string;
  account_label?: string | null;
}

/**
 * Mask an opaque account id for display. 64-hex credential-hash ids are
 * shown as `72ca8b00…a9f5`; emails, UUIDs, and the `default` sentinel pass
 * through unchanged (they are already human-meaningful identities).
 * Navigation / API paths must keep using the full id — this is display-only.
 */
export function maskAccountId(accountId: string): string {
  if (/^[0-9a-f]{64}$/i.test(accountId)) {
    return `${accountId.slice(0, 8)}…${accountId.slice(-4)}`;
  }
  return accountId;
}

/** Human-readable name for a provider account row. */
export function displayAccountName(account: AccountLike): string {
  const label = (account.account_label ?? '').trim();
  if (label !== '') return label;
  if (account.account_id === 'default') return 'Default account';
  return maskAccountId(account.account_id);
}

/**
 * Subtitle (account_id) shown below the display name when the label is
 * distinct from the account_id (otherwise the label already covers the
 * identity and a subtitle would be a duplicate). Returns null when there
 * is no meaningful second line — either no account_id, the special
 * "default" sentinel, or the label already equals the account_id (no
 * label, or label set to the id verbatim, in which case the display
 * name line already shows it).
 */
export function accountSubtitle(account: AccountLike): string | null {
  if (!account.account_id || account.account_id === 'default') return null;
  const label = (account.account_label ?? '').trim();
  if (label === account.account_id) return null;
  if (label === '') {
    // No label: `displayAccountName` falls through to `account_id`, so
    // the display-name line already shows it. A subtitle would be a
    // duplicate. (Replaces the dead-code regex branches the previous
    // version carried — proven unreachable by mutation on PR #294.)
    return null;
  }
  return maskAccountId(account.account_id);
}

/**
 * Display name for a Token Health row. Synthetic ids (`server`,
 * `config:<account>`, `config-cookie:<account>`) map back to the account they
 * stand for; opaque hashes are masked like everywhere else.
 */
export function credentialAccountName(accountId: string, label?: string | null): string {
  const trimmed = (label ?? '').trim();
  if (trimmed !== '') return trimmed;
  if (accountId === 'server') return 'Server environment';
  const m = /^config(?:-cookie)?:(.+)$/.exec(accountId);
  if (m) return m[1] === 'default' ? 'Default account' : m[1];
  return maskAccountId(accountId);
}
