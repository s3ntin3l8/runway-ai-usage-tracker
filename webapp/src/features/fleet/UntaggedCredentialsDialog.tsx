// Operator-side resolver for the silent-listener protocol (PR #288).
//
// The fleet view surfaces credentials the sidecar reported via
// `/api/v1/fleet/credentials/manifest` but no operator has tagged yet.
// For each row, the operator selects a known account (configured or
// discovered, scoped to the row's provider_id) and the dialog POSTs to
// `/api/v1/fleet/credentials/tags`. The server persists the
// CredentialTag, deletes the matching PendingCredentialTag, and writes
// an audit row — the sidecar picks up the new tag via
// `/fleet/config`'s account_tag_hints on its next cycle and starts
// stamping cards under the chosen account_id.
//
// Scope toggle (#319): a dialog-level "This machine / All machines"
// switch sets the request's `scope` field. "This machine" (default)
// scopes the tag to the reporting sidecar; "All machines" persists a
// deployment-wide (sidecar_id NULL) tag that applies to every host —
// useful for shared credential origins (e.g. NFS home dirs). Tagging
// "All machines" also drops any machine-scoped override for the origin,
// so the new mapping wins everywhere. The dialog-level switch applies to every row in that open —
// multi-machine deployments tag per machine by re-opening the dialog
// per entry (singleEntry).
//
// No free-form label entry: the dialog maps to a configured or discovered
// account already known to the server. Empty dropdown state links to the
// provider-config form so an account can be established before tagging.

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { toast } from 'sonner';
import { AlertTriangle } from 'lucide-react';

import {
  fetchProviderConfigs,
  fetchUntaggedCredentials,
  tagCredential,
} from '@/api/endpoints';
import type {
  CredentialTagRequest,
  ProviderAccount,
  UntaggedCredential,
} from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { buildSidecarNameMap, useSidecars } from './queries';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { ClaudeSourceOrganizationPicker } from './ClaudeSourceOrganizationPicker';
import { maskAccountId } from '@/lib/accountDisplay';
import {
  describeCredentialOrigin,
  originTitleWithApp,
  originTitleWithDetail,
  type CredentialOriginKind,
} from '@/lib/credentialOrigin';
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';

interface UntaggedCredentialsDialogProps {
  /** When set, scope the dialog to one sidecar's pending set (per-card entry point). */
  sidecarId?: string;
  /** When set with sidecarId, show only pending credentials for this provider. */
  providerId?: string;
  /** When provided, render only this single entry. Used when a banner row or per-card
   *  badge wants to drive a single-row resolution. */
  singleEntry?: UntaggedCredential;
  open: boolean;
  onClose: () => void;
}

interface DialogState {
  sidecar_id: string;
  provider_id: string;
  credential_origin: string;
  account_id: string;
  // Scope of the staged tag, per row: false = "This machine" (scope: 'sidecar', the
  // server default); true = "All machines" (scope: 'deployment'). Staging a different
  // row starts it back at "This machine", so one broad choice never carries over.
  all_machines: boolean;
}

const INITIAL: DialogState = {
  sidecar_id: '',
  provider_id: '',
  credential_origin: '',
  account_id: '',
  all_machines: false,
};

export function UntaggedCredentialsDialog({
  sidecarId,
  providerId,
  singleEntry,
  open,
  onClose,
}: UntaggedCredentialsDialogProps) {
  const [state, setState] = useState<DialogState>(INITIAL);

  const untagged = useQuery({
    queryKey: ['fleet', 'untagged_credentials', sidecarId ?? 'all'],
    queryFn: () => fetchUntaggedCredentials(sidecarId),
    enabled: open,
  });

  const providerConfigs = useQuery({
    queryKey: ['system', 'provider-configs'],
    queryFn: fetchProviderConfigs,
    enabled: open,
  });

  // Reset the staged tag each time the dialog reopens or the row changes.
  useEffect(() => {
    if (!open) return;
    if (singleEntry) {
      setState({
        sidecar_id: singleEntry.sidecar_id,
        provider_id: singleEntry.provider_id,
        credential_origin: singleEntry.credential_origin,
        account_id: '',
        all_machines: false,
      });
    } else {
      setState(INITIAL);
    }
  }, [open, singleEntry?.sidecar_id, singleEntry?.provider_id, singleEntry?.credential_origin]);

  const entries = untagged.data?.items ?? [];
  // Three entry points use this list: the Fleet banner shows all entries,
  // per-entry actions show one credential, and identity rows filter to a
  // provider on one sidecar. The client filter also guards against entries
  // outside the requested scope if a response ever contains them.
  const visibleEntries = useMemo(
    () =>
      singleEntry
        ? entries.filter(
            (e) =>
              e.credential_origin === singleEntry.credential_origin &&
              e.sidecar_id === singleEntry.sidecar_id,
          )
        : entries.filter(
            (e) =>
              (!sidecarId || e.sidecar_id === sidecarId) &&
              (!providerId || e.provider_id === providerId),
          ),
    [entries, providerId, sidecarId, singleEntry],
  );

  // Per-account rows scoped to each entry's provider_id. The
  // ``ProviderConfig`` response is provider-level — each provider's
  // ``accounts`` field is the per-row list we actually let the
  // operator pick from. Computed once per provider_configs fetch.
  const accountsByProvider = useMemo<Record<string, ProviderAccount[]>>(() => {
    const list = providerConfigs.data?.providers ?? [];
    const by_provider: Record<string, ProviderAccount[]> = {};
    for (const p of list) {
      if (p.accounts?.length) {
        by_provider[p.provider_id] = p.accounts;
      }
    }
    return by_provider;
  }, [providerConfigs.data]);

  const machineNames = buildSidecarNameMap(useSidecars().data?.sidecars ?? []);
  const invalidateCredentialViews = useInvalidateCredentialViews();
  const save = useMutation({
    mutationFn: (body: CredentialTagRequest) => tagCredential(body),
    onSuccess: (data, body) => {
      // A mapping onto a retired account is parked: it resolves the credential
      // but collects nothing until the account is restored. Say so — the
      // operator must not read "tagged" as "collecting now".
      toast.success(
        data.target_archived
          ? `Credential parked on ${body.provider_id} · ${maskAccountId(body.account_id)} (archived)`
          : 'Credential tagged',
      );
      // Tagging changes the untagged list, the rules, the sidecars' identities,
      // token health and the provider cards — refresh them together.
      invalidateCredentialViews();
      if (singleEntry) {
        onClose();
      } else {
        // Several credentials may be waiting: stay open on the next one instead of
        // forcing the operator to reopen the dialog for each.
        setState(INITIAL);
      }
    },
    onError: (err: Error) => {
      toast.error(err.message);
    },
  });

  // Stage a change on one row. Switching to a different row drops the previous row's
  // account and scope, so a pick or an "All machines" choice never lands on another row.
  const stage = (
    entry: UntaggedCredential,
    patch: { accountId?: string; allMachines?: boolean },
  ) =>
    setState((prev) => {
      const sameRow =
        prev.sidecar_id === entry.sidecar_id &&
        prev.provider_id === entry.provider_id &&
        prev.credential_origin === entry.credential_origin;
      return {
        sidecar_id: entry.sidecar_id,
        provider_id: entry.provider_id,
        credential_origin: entry.credential_origin,
        account_id: patch.accountId ?? (sameRow ? prev.account_id : ''),
        all_machines: patch.allMachines ?? (sameRow ? prev.all_machines : false),
      };
    });

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title="Untagged credentials"
      description={
        singleEntry
          ? `${singleEntry.provider_id} · ${sourceLabel(singleEntry)}`
          : providerId
            ? `${providerId} · credentials reported by ${machineNames.get(sidecarId ?? '') ?? sidecarId}`
            : "Choose an account for each credential the sidecar couldn't identify."
      }
    >
      {visibleEntries.length === 0 ? (
        <p className="text-sm text-fg-muted">
          No credentials waiting for a tag. Sidecars that ship with no unresolved origin drop off this list on their next
          heartbeat.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {visibleEntries.map((entry) => (
            <UntaggedRow
              key={`${entry.sidecar_id}/${entry.provider_id}/${entry.source_id ?? entry.credential_origin}`}
              entry={entry}
              machineName={machineNames.get(entry.sidecar_id)}
              accounts={accountsByProvider[entry.provider_id] ?? []}
              currentSelection={state}
              onSelect={(accountId) => stage(entry, { accountId })}
              onScope={(allMachines) => stage(entry, { allMachines })}
              onSave={() => {
                const body = stageToBody(entry, state);
                if (body) save.mutate(body);
              }}
              saving={save.isPending}
              onOrganizationComplete={() => { invalidateCredentialViews(); if (singleEntry) onClose(); }}
              onNavigate={onClose}
            />
          ))}
        </div>
      )}

      <p className="mt-3 text-[11px] text-fg-subtle">
        Tagging persists an operator-resolved mapping the sidecar consumes on its next heartbeat. The audit
        log records the action with credential_origin + sidecar_id + scope only (no plaintext credentials).
      </p>

      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
      </div>
    </ResponsiveDialog>
  );
}

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

interface UntaggedRowProps {
  entry: UntaggedCredential;
  /** Display name of the reporting machine; falls back to the raw sidecar id. */
  machineName?: string;
  accounts: ProviderAccount[];
  currentSelection: DialogState;
  onSelect: (accountId: string) => void;
  onScope: (allMachines: boolean) => void;
  onSave: () => void;
  saving: boolean;
  onOrganizationComplete: () => void;
  /** Called when a link leaves the dialog (it is opened from Provider Settings itself,
   *  so a same-route link would otherwise leave it open and look dead). */
  onNavigate: () => void;
}

function UntaggedRow({
  entry,
  machineName,
  accounts,
  currentSelection,
  onSelect,
  onScope,
  onSave,
  saving,
  onOrganizationComplete,
  onNavigate,
}: UntaggedRowProps) {
  // Each row tracks its own Select state via currentSelection.account_id
  // when the entry matches. We keep that gating intentionally simple —
  // one staged selection at a time per dialog open.
  const isActive =
    currentSelection.provider_id === entry.provider_id &&
    currentSelection.credential_origin === entry.credential_origin &&
    currentSelection.sidecar_id === entry.sidecar_id;

  const selectedAccountId = isActive ? currentSelection.account_id : '';
  // Browser cookies and keychain entries belong to one machine, so an "All machines" tag
  // would follow an account switch on a host it was never made for (the server refuses it).
  const machineBound = isMachineBoundOrigin(entry.credential_origin);
  const allMachines = isActive && currentSelection.all_machines && !machineBound;
  // A key-scoped origin carries a `#fingerprint`; a plain path/env origin is shared as-is.
  const isShared = allMachines && !entry.credential_origin.includes('#');

  // The accounts this credential can be mapped to, in two groups:
  //
  // - active: enabled and not archived — the normal case.
  // - archived: the operator retired the account. Mapping here *parks* the
  //   credential: the binding is recorded (the origin stops being pending and
  //   the operator is not nagged) but nothing is collected until the account
  //   is restored, which revives the binding automatically.
  //
  // Disabled-but-not-archived rows stay unpickable: tagging to one stores
  // a hint the server won't collect (the sidecar's
  // ``accounts`` view in /fleet/config only ships enabled rows).
  // PR #290 round-2 review (Hermes body suggestion #4).
  const activeAccounts = accounts.filter((a) => !a.archived && a.enabled !== false);
  const archivedAccounts = accounts.filter((a) => a.archived);
  const disabledAccounts = accounts.filter((a) => !a.archived && a.enabled === false);
  const pickableAccounts = [...activeAccounts, ...archivedAccounts];
  // The copy shown when there is no account to map to at all.
  const notice = accountPickNotice(disabledAccounts.length);
  // The copy shown when the only options are retired accounts.
  const parkedNotice = archivedAccountNotice(archivedAccounts.length);

  return (
    <Card
      className="p-3"
      role="group"
      aria-label={`${sourceLabel(entry)} on ${machineName ?? entry.sidecar_id}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold">
            {entry.provider_id}
            {sourceDescription(entry).label ? (
              <span className="text-[11px] font-normal text-fg-muted">
                {' '}
                {sourceLabel(entry)}
              </span>
            ) : null}
          </p>
          <p className="truncate text-[11px] text-fg-subtle">
            <span className="font-mono">{displayOrigin(entry.credential_origin)}</span>
            {' · from '}
            <span className="font-mono">{machineName ?? entry.sidecar_id}</span>
            {allMachines && <span className="text-accent"> · applies to all machines</span>}
          </p>
          {sourceDescription(entry).kind === 'cookie' ? (
            <p className="text-[11px] text-fg-subtle">
              A browser session cookie the machine scraped — not a credential file.
            </p>
          ) : null}
          {entry.claimed_account_id && (
            <p className="text-[11px] text-fg-subtle">
              Discovered login: <span className="font-mono">{entry.claimed_account_id}</span>
            </p>
          )}
        </div>
      </div>

      {entry.quota_preview_stale ? (
        <p className="mt-2 rounded-sm bg-surface-2 px-2.5 py-2 text-[11px] text-fg-subtle" role="status">
          The quota preview expired. A fresh preview will appear after the next successful check.
          {entry.quota_preview_observed_at ? (
            <> Last observed {new Date(entry.quota_preview_observed_at).toLocaleString()}.</>
          ) : null}
        </p>
      ) : null}

      {entry.quota_preview?.length && !entry.quota_preview_stale ? (
        <div className="mt-2 rounded-sm bg-surface-2 px-2.5 py-2 text-[11px]">
          <p className="font-medium">Live quota from this credential</p>
          {entry.quota_preview_observed_at ? (
            <p className="text-fg-subtle">
              Observed {new Date(entry.quota_preview_observed_at).toLocaleString()}
            </p>
          ) : null}
          <ul className="mt-1 flex flex-col gap-0.5 text-fg-subtle">
            {entry.quota_preview.map((quota, index) => (
              <li key={`${quota.service_name ?? 'quota'}-${quota.window_type ?? index}`}>
                {quota.service_name ?? entry.provider_id}
                {quota.window_type ? ` · ${quota.window_type}` : ''}
                {quota.remaining !== undefined
                  ? ` · ${quota.remaining}${quota.unit ? ` ${quota.unit}` : ''}`
                  : ''}
                {quota.pct_used !== undefined ? ` · ${quota.pct_used}% used` : ''}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-fg-subtle">Not added to account history until assigned.</p>
        </div>
      ) : null}

      {isClaudeBrowserSource(entry) ? (
        <ClaudeSourceOrganizationPicker sourceId={entry.source_id!} providerId={entry.provider_id} onComplete={onOrganizationComplete} />
      ) : pickableAccounts.length === 0 ? (
        <p className="mt-2 flex items-start gap-2 text-[12px] text-warning">
          <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
          {/* The whole message lives in one span: as flex children of this <p>, the
              text runs, the provider id and the link would each become their own
              flex item and wrap independently (ragged, gappy layout). One span
              keeps it a single flowing sentence next to the icon. */}
          <span className="min-w-0">
            {notice.lead}{' '}
            <span className="font-mono">{entry.provider_id}</span>{' '}
            {notice.tail}{' '}
            <Link
              to="/settings/providers"
              onClick={onNavigate}
              className="text-accent underline underline-offset-2"
            >
              {notice.linkLabel}
            </Link>
            .
          </span>
        </p>
      ) : (
        <div className="mt-3 flex items-end gap-2">
          <div className="min-w-0 flex-1">
            <Label htmlFor={`tag-${entry.sidecar_id}-${entry.credential_origin}`}>Map to</Label>
            <Select
              value={selectedAccountId}
              onValueChange={onSelect}
            >
              <SelectTrigger
                id={`tag-${entry.sidecar_id}-${entry.credential_origin}`}
                className="w-full"
              >
                <SelectValue placeholder="Pick an account…" />
              </SelectTrigger>
              <SelectContent>
                {activeAccounts.map((a) => (
                  <SelectItem
                    key={`${entry.provider_id}/${a.account_id}`}
                    value={a.account_id}
                  >
                    {a.account_label || maskAccountId(a.account_id)}
                    {a.account_label ? ` · ${maskAccountId(a.account_id)}` : ''}
                  </SelectItem>
                ))}
                {archivedAccounts.length > 0 ? (
                  <SelectGroup>
                    <SelectLabel>Archived · parked until restored</SelectLabel>
                    {archivedAccounts.map((a) => (
                      <SelectItem
                        key={`${entry.provider_id}/${a.account_id}`}
                        value={a.account_id}
                      >
                        {a.account_label || maskAccountId(a.account_id)}
                        {a.account_label ? ` · ${maskAccountId(a.account_id)}` : ''}
                        {' · archived'}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                ) : null}
              </SelectContent>
            </Select>
          </div>
          <Button
            variant="primary"
            onClick={onSave}
            disabled={!selectedAccountId}
            loading={saving}
          >
            Tag
          </Button>
        </div>
      )}

      {activeAccounts.length === 0 && archivedAccounts.length > 0 ? (
        <p className="mt-2 text-[12px] text-fg-subtle">
          {parkedNotice.lead}{' '}
          <span className="font-mono">{entry.provider_id}</span>{' '}
          {parkedNotice.tail}{' '}
          <Link
            to="/settings/providers"
            onClick={onNavigate}
            className="text-accent underline underline-offset-2"
          >
            {parkedNotice.linkLabel}
          </Link>
          .
        </p>
      ) : null}

      {pickableAccounts.length > 0 && !isClaudeBrowserSource(entry) ? (
        <div className="mt-2">
          <div className="flex items-center justify-between gap-3">
            <p className="text-[11px] text-fg-subtle">
              {machineBound
                ? 'Browser and keychain credentials belong to one machine, so the tag applies only to the sidecar that reported it.'
                : allMachines
                  ? 'Applies to every sidecar that reports this credential.'
                  : 'Applies only to the sidecar that reported it.'}
            </p>
            <div
              className="flex shrink-0 rounded-md border border-border bg-surface-1 p-0.5"
              role="radiogroup"
              aria-label={`Tag scope for ${displayOrigin(entry.credential_origin)} on ${machineName ?? entry.sidecar_id}`}
            >
              <button
                type="button"
                role="radio"
                aria-checked={!allMachines}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                  !allMachines ? 'bg-accent text-fg-inverse' : 'text-fg-muted hover:text-fg'
                }`}
                onClick={() => onScope(false)}
              >
                This machine
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={allMachines}
                disabled={machineBound}
                title={machineBound ? 'Not available for browser or keychain credentials' : undefined}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                  allMachines ? 'bg-accent text-fg-inverse' : 'text-fg-muted hover:text-fg'
                }`}
                onClick={() => onScope(true)}
              >
                All machines
              </button>
            </div>
          </div>
          {isShared ? (
            <p className="mt-1 text-[11px] text-warning">
              This rule would apply on every machine and isn&apos;t tied to the credential itself —
              if the account behind it changes, data keeps landing on the old one.
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function isClaudeBrowserSource(entry: UntaggedCredential): boolean {
  return Boolean(entry.source_id) && entry.credential_origin.startsWith('cookie:') && ['anthropic', 'anthropic_console'].includes(entry.provider_id);
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
