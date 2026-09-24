// Operator-side resolver for the silent-listener protocol (PR #288).
//
// The fleet view surfaces credentials the sidecar reported via
// `/api/v1/fleet/credentials/manifest` but no operator has tagged yet.
// For each row, the operator selects a configured provider_configs row
// (scoped to the row's provider_id) and the dialog POSTs to
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
// useful for shared credential origins (e.g. NFS home dirs) and for
// retagging a known origin from a second machine without visiting the
// first. The dialog-level switch applies to every row in that open —
// multi-machine deployments tag per machine by re-opening the dialog
// per entry (singleEntry).
//
// No free-form label entry: the dialog always maps to an existing
// provider_configs row. Empty dropdown state links to the existing
// provider-config form (precondition: identity must exist server-side
// before tagging).

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
}

const INITIAL: DialogState = {
  sidecar_id: '',
  provider_id: '',
  credential_origin: '',
  account_id: '',
};

export function UntaggedCredentialsDialog({
  sidecarId,
  singleEntry,
  open,
  onClose,
}: UntaggedCredentialsDialogProps) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<DialogState>(INITIAL);
  // #319 scope: false = "This machine" (scope: 'sidecar', the server
  // default); true = "All machines" (scope: 'deployment').
  const [applyToAllMachines, setApplyToAllMachines] = useState(false);

  const untagged = useQuery({
    queryKey: ['fleet', 'untagged_credentials', sidecarId ?? 'all'],
    queryFn: () => fetchUntaggedCredentials(sidecarId),
    enabled: open,
  });

  const providerConfigs = useQuery({
    queryKey: ['system', 'provider_configs'],
    queryFn: fetchProviderConfigs,
    enabled: open,
  });

  // Reset the staged tag each time the dialog reopens or the row changes.
  useEffect(() => {
    if (!open) return;
    setApplyToAllMachines(false);
    if (singleEntry) {
      setState({
        sidecar_id: singleEntry.sidecar_id,
        provider_id: singleEntry.provider_id,
        credential_origin: singleEntry.credential_origin,
        account_id: '',
      });
    } else {
      setState(INITIAL);
    }
  }, [open, singleEntry?.sidecar_id, singleEntry?.provider_id, singleEntry?.credential_origin]);

  const entries = untagged.data?.items ?? [];
  // When invoked as a per-card / single-entry dialog, only show the
  // targeted entry; otherwise show every pending entry.
  const visibleEntries = useMemo(
    () => (singleEntry ? entries.filter((e) => e.credential_origin === singleEntry.credential_origin && e.sidecar_id === singleEntry.sidecar_id) : entries),
    [entries, singleEntry],
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

  const save = useMutation({
    mutationFn: (body: CredentialTagRequest) => tagCredential(body),
    onSuccess: () => {
      toast.success('Credential tagged');
      // Invalidate both the untagged list (the entry disappears) and
      // the fleet list (counts may change in any card).
      queryClient.invalidateQueries({ queryKey: ['fleet', 'untagged_credentials'] });
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
      queryClient.invalidateQueries({ queryKey: ['system', 'provider_configs'] });
      onClose();
    },
    onError: (err: Error) => {
      toast.error(err.message);
    },
  });

  const stage = (entry: UntaggedCredential, accountId: string) =>
    setState({
      sidecar_id: entry.sidecar_id,
      provider_id: entry.provider_id,
      credential_origin: entry.credential_origin,
      account_id: accountId,
    });

  const scopeLabel = applyToAllMachines ? 'All machines' : 'This machine';

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
          : "Pick a configured provider row for each credential the sidecar couldn't identify."
      }
    >
      {visibleEntries.length === 0 ? (
        <p className="text-sm text-fg-muted">
          No credentials waiting for a tag. Sidecars that ship with no unresolved origin drop off this list on their next
          heartbeat.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {/* #319 scope toggle — dialog-level; stageToBody reads the
              current switch when the Tag button is clicked. */}
          <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface-2 px-3 py-2">
            <div className="min-w-0">
              <p className="text-[12px] font-medium">Scope</p>
              <p className="text-[11px] text-fg-subtle">
                {applyToAllMachines
                  ? 'Applies to every sidecar that reports this credential.'
                  : 'Applies only to the sidecar that reported it.'}
              </p>
            </div>
            <div
              className="flex shrink-0 rounded-md border border-border bg-surface-1 p-0.5"
              role="radiogroup"
              aria-label="Tag scope"
            >
              <button
                type="button"
                role="radio"
                aria-checked={!applyToAllMachines}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                  !applyToAllMachines
                    ? 'bg-accent text-fg-inverse'
                    : 'text-fg-muted hover:text-fg'
                }`}
                onClick={() => setApplyToAllMachines(false)}
              >
                This machine
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={applyToAllMachines}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                  applyToAllMachines
                    ? 'bg-accent text-fg-inverse'
                    : 'text-fg-muted hover:text-fg'
                }`}
                onClick={() => setApplyToAllMachines(true)}
              >
                All machines
              </button>
            </div>
          </div>

          {visibleEntries.map((entry) => (
            <UntaggedRow
              key={`${entry.sidecar_id}/${entry.provider_id}/${entry.credential_origin}`}
              entry={entry}
              accounts={accountsByProvider[entry.provider_id] ?? []}
              currentSelection={state}
              scopeLabel={scopeLabel}
              onSelect={(accountId) => stage(entry, accountId)}
              onSave={() =>
                save.mutate(
                  stageToBody(
                    entry,
                    accountsByProvider[entry.provider_id] ?? [],
                    state,
                    applyToAllMachines,
                  ),
                )
              }
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

function stageToBody(
  entry: UntaggedCredential,
  matchingAccounts: ProviderAccount[],
  state: DialogState,
  applyToAllMachines: boolean,
): CredentialTagRequest {
  // The state machine keeps a single staged selection per dialog open.
  // The Save button is rendered per-row, so the body comes from that
  // row's currently staged account_id (state at the time of click).
  // ``scope`` (#319) is the dialog-level switch's value at click time.
  return {
    sidecar_id: entry.sidecar_id,
    provider_id: entry.provider_id,
    credential_origin: entry.credential_origin,
    account_id:
      state.account_id ||
      // Defensive: if the row's account_id wasn't staged yet (e.g. the
      // user clicked Save before the Select rendered), fall back to the
      // first matching account's id so the click isn't a no-op.
      matchingAccounts[0]?.account_id ||
      '',
    scope: applyToAllMachines ? 'deployment' : 'sidecar',
  };
}

interface UntaggedRowProps {
  entry: UntaggedCredential;
  accounts: ProviderAccount[];
  currentSelection: DialogState;
  scopeLabel: string;
  onSelect: (accountId: string) => void;
  onSave: () => void;
  saving: boolean;
}

function UntaggedRow({
  entry,
  accounts,
  currentSelection,
  scopeLabel,
  onSelect,
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

  // Filter out disabled accounts — tagging to a disabled row stores
  // a hint the server won't collect (the sidecar's
  // ``accounts`` view in /fleet/config only ships enabled rows).
  // PR #290 round-2 review (Hermes body suggestion #4).
  const enabledAccounts = accounts.filter((a) => a.enabled !== false);
  const disabledCount = accounts.length - enabledAccounts.length;

  return (
    <Card className="p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold">
            {entry.provider_id}{' '}
            <span className="font-mono text-[11px] text-fg-muted">{entry.credential_origin}</span>
          </p>
          <p className="truncate text-[11px] text-fg-subtle">
            from <span className="font-mono">{entry.sidecar_id}</span>
            {scopeLabel === 'All machines' && (
              <span className="text-accent"> · applies to all machines</span>
            )}
          </p>
        </div>
      </div>

      {enabledAccounts.length === 0 ? (
        <p className="mt-2 flex items-center gap-2 text-[12px] text-warning">
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
          {accounts.length === 0 ? (
            <>
              No <span className="font-mono">{entry.provider_id}</span> row configured.{' '}
              <a
                href={`/settings/providers#${entry.provider_id}`}
                className="text-accent underline underline-offset-2"
              >
                Add one in Provider Settings
              </a>
              .
            </>
          ) : (
            <>
              All {accounts.length}{' '}
              <span className="font-mono">{entry.provider_id}</span> row
              {accounts.length === 1 ? '' : 's'} configured{' '}
              {disabledCount > 0 ? 'are' : 'is'} disabled.{' '}
              <a
                href={`/settings/providers#${entry.provider_id}`}
                className="text-accent underline underline-offset-2"
              >
                Enable in Provider Settings
              </a>
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
                <SelectValue placeholder="Pick a configured account…" />
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
    </Card>
  );
}

// Sentinel re-export so tests can import the helper if they need to.
export { INITIAL as UNTAGGED_DIALOG_INITIAL };
