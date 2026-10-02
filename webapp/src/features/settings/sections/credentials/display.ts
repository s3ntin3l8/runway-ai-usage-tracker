// One vocabulary for credentials, shared by every Credentials view.
//   credential = one secret found in one place   origin  = where it was found
//   machine    = a sidecar                       account = the (provider, id) data is filed under
//   mapping    = why the credential belongs to that account

import type { BadgeProps } from '@/components/ui/Badge';
import type { CredentialMapping, CredentialSourceView, CredentialUnusedReason } from '@/api/types';

export const STATUS_VARIANT: Record<string, BadgeProps['variant']> = {
  valid: 'ok',
  expiring: 'warning',
  expired: 'critical',
  invalid: 'critical',
  stale: 'neutral',
  unknown: 'neutral',
};

export const STATUS_LABEL: Record<string, string> = {
  valid: 'Valid',
  expiring: 'Expiring',
  expired: 'Expired',
  invalid: 'Rejected',
  stale: 'Not reported',
  unknown: 'Unknown',
};

export const STATUS_HINT: Record<string, string> = {
  valid: 'The credential is usable.',
  expiring: 'Expires soon and cannot be rolled automatically.',
  expired: 'The credential has expired.',
  invalid: 'The provider rejected this credential on the last collection.',
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

/** "Browser cookie", "oauth_creds.json", "GITHUB_TOKEN" … plus the machine, if any. */
export function originSummary(s: CredentialSourceView): string {
  if (s.origin_kind === 'machine') return `${s.label} on ${s.machine_name ?? s.machine_id}`;
  if (s.origin_kind === 'server') return `${s.label} (server)`;
  return s.label;
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
