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
import { maskAccountId } from '@/lib/accountDisplay';
import {
  Select,
  SelectContent,
  SelectItem,
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
    onSuccess: () => {
      toast.success('Credential tagged');
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
          ? `${singleEntry.provider_id} · ${singleEntry.credential_origin}`
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
              key={`${entry.sidecar_id}/${entry.provider_id}/${entry.credential_origin}`}
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

  // Filter out disabled accounts — tagging to a disabled row stores
  // a hint the server won't collect (the sidecar's
  // ``accounts`` view in /fleet/config only ships enabled rows).
  // PR #290 round-2 review (Hermes body suggestion #4).
  const enabledAccounts = accounts.filter((a) => a.enabled !== false);
  const disabledCount = accounts.length - enabledAccounts.length;

  return (
    <Card
      className="p-3"
      role="group"
      aria-label={`${entry.credential_origin} on ${machineName ?? entry.sidecar_id}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold">
            {entry.provider_id}{' '}
            <span className="font-mono text-[11px] text-fg-muted">{entry.credential_origin}</span>
          </p>
          <p className="truncate text-[11px] text-fg-subtle">
            from <span className="font-mono">{machineName ?? entry.sidecar_id}</span>
            {allMachines && <span className="text-accent"> · applies to all machines</span>}
          </p>
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

      {enabledAccounts.length === 0 ? (
        <p className="mt-2 flex items-center gap-2 text-[12px] text-warning">
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
          {accounts.length === 0 ? (
            <>
              No <span className="font-mono">{entry.provider_id}</span> row configured.{' '}
              <Link to="/settings/providers" className="text-accent underline underline-offset-2">
                Add one in Provider Settings
              </Link>
              .
            </>
          ) : (
            <>
              All {accounts.length}{' '}
              <span className="font-mono">{entry.provider_id}</span> row
              {accounts.length === 1 ? '' : 's'} configured{' '}
              {disabledCount > 0 ? 'are' : 'is'} disabled.{' '}
              <Link to="/settings/providers" className="text-accent underline underline-offset-2">
                Enable in Provider Settings
              </Link>
              .
            </>
          )}
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
                {enabledAccounts.map((a) => (
                  <SelectItem
                    key={`${entry.provider_id}/${a.account_id}`}
                    value={a.account_id}
                  >
                    {a.account_label || maskAccountId(a.account_id)}
                    {a.account_label ? ` · ${maskAccountId(a.account_id)}` : ''}
                  </SelectItem>
                ))}
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

      {enabledAccounts.length > 0 ? (
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
              aria-label={`Tag scope for ${entry.credential_origin} on ${machineName ?? entry.sidecar_id}`}
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
