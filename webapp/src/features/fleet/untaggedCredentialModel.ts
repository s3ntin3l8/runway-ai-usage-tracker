// Tag-dialog decisions that do not render: which origins are bound to one
// machine, how a missing or archived account is described, and the request
// body a staged row turns into. UntaggedCredentialsDialog re-exports the
// public names so existing imports keep working.

import type { CredentialTagRequest, UntaggedCredential } from '@/api/types';
import {
  describeCredentialOrigin,
  originTitleWithApp,
  originTitleWithDetail,
  type CredentialOriginKind,
} from '@/lib/credentialOrigin';

/** Origins that cannot be shared between machines: a browser's cookie jar or a keychain. */
export function isMachineBoundOrigin(origin: string): boolean {
  return origin.startsWith('cookie:') || origin.startsWith('keychain:');
}

/** The parts of the "no account to map to" notice, split around the provider id
 *  so the id can be rendered in the mono font. */
export interface AccountPickNotice {
  /** Sentence fragment rendered before the provider id ("No", "The", "All 2"). */
  lead: string;
  /** Sentence fragment rendered after the provider id ("row is disabled."). */
  tail: string;
  /** Label of the link to Provider Settings. */
  linkLabel: string;
}

/**
 * The copy for a credential with no enabled account to map to. One helper
 * keeps the grammar — lead noun, verb and link label — coupled to a single
 * count check, so the three pieces cannot drift apart.
 */
export function accountPickNotice(accountCount: number): AccountPickNotice {
  if (accountCount === 0) {
    return {
      lead: 'No',
      tail: 'row is configured.',
      linkLabel: 'Add one in Provider Settings',
    };
  }
  if (accountCount === 1) {
    return {
      lead: 'The',
      tail: 'row is disabled.',
      linkLabel: 'Enable it in Provider Settings',
    };
  }
  return {
    lead: `All ${accountCount}`,
    tail: 'rows are disabled.',
    linkLabel: 'Enable one in Provider Settings',
  };
}

/**
 * The copy for a credential whose provider has retired accounts. An archived
 * account is a legal target: mapping to it *parks* the credential — the
 * binding is recorded so the origin stops being pending and the operator is
 * not nagged — but nothing is collected until the account is restored.
 *
 * This is information, not a warning: the operator has an action, so the copy
 * explains what the action does instead of blocking it.
 */
export function archivedAccountNotice(archivedCount: number): AccountPickNotice {
  const parked =
    'Mapping here parks the credential — nothing is collected until it is restored.';
  if (archivedCount === 1) {
    return {
      lead: 'The',
      tail: `account is archived. ${parked}`,
      linkLabel: 'Restore it in Provider Settings',
    };
  }
  return {
    lead: `All ${archivedCount}`,
    tail: `accounts are archived. ${parked}`,
    linkLabel: 'Restore one in Provider Settings',
  };
}

export interface DialogState {
  sidecar_id: string;
  provider_id: string;
  credential_origin: string;
  account_id: string;
  // Scope of the staged tag, per row: false = "This machine" (scope: 'sidecar', the
  // server default); true = "All machines" (scope: 'deployment'). Staging a different
  // row starts it back at "This machine", so one broad choice never carries over.
  all_machines: boolean;
}

export function stageToBody(
  entry: UntaggedCredential,
  state: DialogState,
): CredentialTagRequest | null {
  // Guard per entry as well as at the dialog level: this is exported and the request must
  // never carry an all-machines scope for a cookie or keychain origin, whoever calls it.
  // The dialog keeps a single staged selection per open, but the Tag
  // button is rendered per row. Only use the staged account when it was
  // staged *for this row* (same sidecar + provider + origin) — otherwise a
  // pick on machine A's row could be saved onto machine B's row for the
  // same origin. No fallback to "the first account": that could silently
  // tag a disabled or unintended account. ``scope`` (#319) is this row's
  // own switch, staged with the account.
  const stagedForEntry =
    state.sidecar_id === entry.sidecar_id &&
    state.provider_id === entry.provider_id &&
    state.credential_origin === entry.credential_origin;
  if (!stagedForEntry || !state.account_id) return null;
  return {
    sidecar_id: entry.sidecar_id,
    provider_id: entry.provider_id,
    credential_origin: entry.credential_origin,
    account_id: state.account_id,
    scope: state.all_machines && !isMachineBoundOrigin(entry.credential_origin) ? 'deployment' : 'sidecar',
  };
}

/**
 * The origin without its fingerprint: the diagnostics line must not print a
 * secret-derived hash (the fingerprint is never displayed).
 */
export function displayOrigin(origin: string): string {
  return origin.split('#', 1)[0];
}

/**
 * The source in words: "Browser cookie", "Keychain entry".
 *
 * A file or env origin ("path:/home/u/.codex/auth.json") already says what it
 * is, so it is left alone. A cookie origin ("cookie:opencode/session") is a
 * protocol string: it looks like a path and hides the fact that this is a
 * browser session rather than another credential file — that one gets named.
 *
 * `label` is undefined when the source is shown by its raw origin instead.
 */
export function sourceDescription(entry: UntaggedCredential): {
  label?: string;
  detail?: string;
  kind: CredentialOriginKind;
} {
  const described = describeCredentialOrigin(entry.credential_origin);
  const kind = entry.credential_origin.startsWith('cookie:')
    ? 'cookie'
    : entry.credential_origin.startsWith('keychain:')
      ? 'keychain'
      : described.kind;
  const serverLabel = entry.origin_label;
  const named =
    serverLabel !== undefined && serverLabel !== '' && serverLabel !== 'Sidecar credential';
  const label =
    kind === 'cookie' || kind === 'keychain'
      ? named && serverLabel
        ? serverLabel
        : described.label
      : undefined;
  const detail = entry.origin_detail ?? described.detail;
  return { label, detail: detail ?? undefined, kind };
}

/** The source as one line: "Chrome · Session cookie", "Browser cookie (Chrome · Profile 1)". */
export function sourceLabel(entry: UntaggedCredential): string {
  const { label, detail } = sourceDescription(entry);
  if (!label) return displayOrigin(entry.credential_origin);
  return originTitleWithDetail(originTitleWithApp(entry.origin_app, label), detail);
}
