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
  ProviderConfig,
  UntaggedCredential,
} from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
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

  // Provider rows scoped to each entry's provider_id. Computed once
  // per provider_configs fetch. The dropdown's available options match
  // the entry, not the dialog-level staged state — the staged state
  // only controls which row's Tag button is enabled.
  const providerConfigsByProvider = useMemo<Record<string, ProviderConfig[]>>(() => {
    const list = providerConfigs.data?.providers ?? [];
    const by_provider: Record<string, ProviderConfig[]> = {};
    for (const p of list) {
      (by_provider[p.provider_id] ??= []).push(p);
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
          {visibleEntries.map((entry) => (
            <UntaggedRow
              key={`${entry.sidecar_id}/${entry.provider_id}/${entry.credential_origin}`}
              entry={entry}
              providerConfigs={providerConfigsByProvider[entry.provider_id] ?? []}
              currentSelection={state}
              onSelect={(accountId) => stage(entry, accountId)}
              onSave={() =>
                save.mutate(
                  stageToBody(
                    entry,
                    providerConfigsByProvider[entry.provider_id] ?? [],
                    state,
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
        log records the action with credential_origin + sidecar_id only (no plaintext credentials).
      </p>

      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
      </div>
    </ResponsiveDialog>
  );
}

function stageToBody(
  entry: UntaggedCredential,
  matchingProviderConfigs: ProviderConfig[],
  state: DialogState,
): CredentialTagRequest {
  // The state machine keeps a single staged selection per dialog open.
  // The Save button is rendered per-row, so the body comes from that
  // row's currently staged account_id (state at the time of click).
  return {
    sidecar_id: entry.sidecar_id,
    provider_id: entry.provider_id,
    credential_origin: entry.credential_origin,
    account_id:
      state.account_id ||
      // Defensive: if the row's account_id wasn't staged yet (e.g. the
      // user clicked Save before the Select rendered), fall back to the
      // first matching row's account_id so the click isn't a no-op.
      matchingProviderConfigs[0]?.account_id ||
      '',
  };
}

interface UntaggedRowProps {
  entry: UntaggedCredential;
  providerConfigs: ProviderConfig[];
  currentSelection: DialogState;
  onSelect: (accountId: string) => void;
  onSave: () => void;
  saving: boolean;
}

function UntaggedRow({
  entry,
  providerConfigs,
  currentSelection,
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
          </p>
        </div>
      </div>

      {providerConfigs.length === 0 ? (
        <p className="mt-2 flex items-center gap-2 text-[12px] text-warning">
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
          No <span className="font-mono">{entry.provider_id}</span> row configured.{' '}
          <a
            href={`/settings/providers#${entry.provider_id}`}
            className="text-accent underline underline-offset-2"
          >
            Add one in Provider Settings
          </a>
          .
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
                {providerConfigs.map((p) => (
                  <SelectItem
                    key={`${p.provider_id}/${p.account_id}`}
                    value={p.account_id}
                  >
                    {p.account_label || p.account_id}
                    {p.account_label ? ` · ${p.account_id}` : ''}
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
