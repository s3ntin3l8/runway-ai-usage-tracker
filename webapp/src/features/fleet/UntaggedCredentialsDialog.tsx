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
import { toast } from 'sonner';

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
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { maskAccountId } from '@/lib/accountDisplay';
import { buildSidecarNameMap, sidecarLabel, useSidecars } from './queries';
import { UntaggedRow } from './UntaggedRow';
import {
  accountPickNotice,
  archivedAccountNotice,
  displayOrigin,
  isMachineBoundOrigin,
  sourceDescription,
  sourceLabel,
  stageToBody,
  type AccountPickNotice,
  type DialogState,
} from './untaggedCredentialModel';

// Tests and NeedsMappingView import these from this module.
export type { AccountPickNotice };
export {
  isMachineBoundOrigin,
  accountPickNotice,
  archivedAccountNotice,
  stageToBody,
  displayOrigin,
  sourceDescription,
  sourceLabel,
};

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
            ? `${providerId} · credentials reported by ${sidecarLabel(machineNames, sidecarId ?? '')}`
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
