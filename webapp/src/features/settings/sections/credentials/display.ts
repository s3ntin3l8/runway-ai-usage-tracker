// One vocabulary for credentials, shared by every Credentials view.
//   credential = one secret found in one place   origin  = where it was found
//   machine    = a sidecar                       account = the (provider, id) data is filed under
//   mapping    = why the credential belongs to that account

import type { BadgeProps } from '@/components/ui/Badge';
import type {
  CredentialAccountView,
  CredentialMapping,
  CredentialSourceView,
  CredentialUnusedReason,
} from '@/api/types';
import { maskAccountId } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';

export const STATUS_VARIANT: Record<string, BadgeProps['variant']> = {
  valid: 'ok',
  expiring: 'warning',
  expired: 'critical',
  invalid: 'critical',
  failing: 'critical',
  stale: 'neutral',
  unknown: 'neutral',
};

export const STATUS_LABEL: Record<string, string> = {
  valid: 'Valid',
  expiring: 'Expiring',
  expired: 'Expired',
  invalid: 'Rejected',
  failing: 'Failing',
  stale: 'Not reported',
  unknown: 'Unknown',
};

export const STATUS_HINT: Record<string, string> = {
  valid: 'The credential is usable.',
  expiring: 'Expires soon and cannot be rolled automatically.',
  expired: 'The credential has expired.',
  invalid: 'The provider rejected this credential on the last collection.',
  failing: 'The last several collections with this credential failed (the provider did not reject it).',
  stale: 'The machine stopped reporting this credential; its stored state is out of date.',
  unknown: 'No expiry information is available.',
};

export const MAPPING_LABEL: Record<CredentialMapping, string> = {
  local: 'Identified on machine',
  verified: 'Verified by server',
  claim: 'Claimed by machine',
  operator: 'Assigned by you',
  rotation: 'Carried over after re-login',
  config: 'Saved in Settings',
  server: 'Server environment',
  pending: 'Needs an account',
};

export const MAPPING_HINT: Record<CredentialMapping, string> = {
  local: 'The machine read the account identity from the credential itself.',
  verified: 'The server called the provider and confirmed which account this is.',
  claim: 'The machine claimed this identity; the server accepted it.',
  operator: 'You assigned this credential to the account with a rule.',
  rotation:
    'A re-login re-keyed this credential; the binding you made for its previous key was carried over because this location has only ever belonged to this one account.',
  config: 'Pasted into Settings → Providers.',
  server: 'Found in an environment variable or file on the server host.',
  pending: 'No account is known yet. Assign one under "Needs mapping".',
};

/** "Codex CLI · auth.json", "Browser cookie", "GITHUB_TOKEN" — what a credential is. */
export function originTitle(s: CredentialSourceView): string {
  if (s.origin_app && s.origin_type !== 'cookie') return `${s.origin_app} · ${s.label}`;
  return s.label;
}

/** "Codex CLI · auth.json on dev-01", "GITHUB_TOKEN (server)" — plus the machine, if any. */
export function originSummary(s: CredentialSourceView): string {
  if (s.origin_kind === 'machine') return `${originTitle(s)} on ${s.machine_name ?? s.machine_id}`;
  if (s.origin_kind === 'server') return `${originTitle(s)} (server)`;
  return originTitle(s);
}

/** "OAuth + refresh", "API key", "Cookie" — the raw token types stay in a tooltip. */
export function tokenSummary(types: string[]): string {
  if (types.length === 0) return '';
  const has = (t: string) => types.includes(t);
  if (has('oauth_token') || has('access_token')) {
    return has('refresh_token') ? 'OAuth + refresh' : 'OAuth';
  }
  if (has('api_key')) return 'API key';
  if (types.some((t) => t.includes('cookie') || t.includes('session'))) return 'Cookie';
  return types.join(', ');
}

/** A credential that is not feeding data and needs attention (or removal), never the active one. */
export function isInactive(s: CredentialSourceView): boolean {
  return (
    !s.is_active &&
    (s.status === 'expired' ||
      s.status === 'invalid' ||
      s.status === 'failing' ||
      s.status === 'stale')
  );
}

/** "in 3d", "2h ago", "—" */
export function relativeExpiry(seconds: number | null | undefined): string {
  if (seconds == null) return 'no expiry';
  const abs = Math.abs(seconds);
  const unit =
    abs >= 86_400
      ? `${Math.floor(abs / 86_400)}d`
      : abs >= 3_600
        ? `${Math.floor(abs / 3_600)}h`
        : `${Math.max(1, Math.floor(abs / 60))}m`;
  return seconds < 0 ? `expired ${unit} ago` : `in ${unit}`;
}

export const UNUSED_HINT: Record<CredentialUnusedReason, string> = {
  provider_disabled: 'Collection for this provider is turned off, so this credential is not used.',
  default_disabled: 'The default account is disabled, so this credential is not used.',
  account_keyed_config:
    'Every account for this provider is configured by name, so the server never reads this environment credential. Add a "default" account to use it.',
  shadowed_by_config_key:
    'A key saved in Settings → Providers takes precedence, so this one is not read.',
};

/** For text that leaves the page: an email keeps its domain only ("a***@example.com"). */
function redactAccountId(accountId: string): string {
  const at = accountId.indexOf('@');
  return at > 0 ? `${accountId[0]}***${accountId.slice(at)}` : maskAccountId(accountId);
}

/**
 * A plain-text summary of one account's credentials for a bug report. The inventory holds no
 * secrets and origin paths are already `~`-collapsed; the account email is redacted.
 */
export function diagnosticsText(providerName: string, account: CredentialAccountView): string {
  const active = account.sources.find((s) => s.source_id === account.active_source_id);
  const via = [account.data_source, account.input_source].filter(Boolean).join(' / ');
  const lines = [
    `Runway credentials — ${providerName}`,
    `Account: ${account.identity_pending ? 'needs an account' : redactAccountId(account.account_id)}`,
    `Status: ${STATUS_LABEL[account.status] ?? account.status}`,
    `Data from: ${active ? originSummary(active) : 'no successful collection yet'}${via ? ` (${via})` : ''}`,
    `Credentials (${account.sources.length}):`,
  ];
  for (const s of account.sources) {
    const where =
      s.origin_kind === 'machine'
        ? `${s.machine_name ?? s.machine_id ?? 'machine'}${s.machine_stale ? ' (offline)' : ''}`
        : s.origin_kind === 'server'
          ? 'server'
          : 'settings';
    const parts = [
      `${originTitle(s)} on ${where}`,
      STATUS_LABEL[s.status] ?? s.status,
      s.status === 'stale' ? `last reported ${timeAgo(s.last_seen)}` : relativeExpiry(s.expires_in_seconds),
      s.last_success_at ? `collected ${timeAgo(s.last_success_at)}` : 'never collected',
      MAPPING_LABEL[s.mapping],
    ];
    if (s.is_active) parts.push('active');
    if (!s.enabled) parts.push('disabled');
    if (s.shared_with?.length) {
      const offline = s.shared_with_stale ?? [];
      const peers = s.shared_with.map((n) => (offline.includes(n) ? `${n} (offline)` : n));
      parts.push(`same secret on ${peers.join(', ')}`);
    }
    if (s.last_error) parts.push(`last error: ${s.last_error}`);
    lines.push(`- ${parts.join(' · ')}`);
  }
  return lines.join('\n');
}
