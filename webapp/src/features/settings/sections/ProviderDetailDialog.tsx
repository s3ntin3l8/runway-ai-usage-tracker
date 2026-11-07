// Per-provider detail dialog. Shows the master enabled toggle + a vertical
// list of account rows for one ProviderConfig. Each row exposes a "⋮" menu
// with Edit / Remove actions. The "Add account" footer button is wired in
// #287 (wizard lands); #286 ships it disabled with a tooltip pointing at
// the follow-up PR.
//

import { useId, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { deleteProviderConfig, putDiscoveredAccountLabel, putProviderConfig } from '@/api/endpoints';
import type { ProviderAccount, ProviderConfig } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { displayAccountName, maskAccountId } from '@/lib/accountDisplay';
import { ProviderAccountDialog } from './ProviderAccountDialog';
import { ProviderDetailAccountList } from './ProviderDetailAccountList';
import { ClaudeCloudCollectorSwitch } from './ClaudeCloudCollectorSwitch';
import { ProviderDetailHeader } from './ProviderDetailHeader';

interface ProviderDetailDialogProps {
  provider: ProviderConfig | null;
  connections?: ProviderConfig[];
  onSelectConnection?: (providerId: string) => void;
  onClose: () => void;
  onAccountDeleted?: (providerId: string, accountId: string) => void;
  // PR #287 — when the user clicks "Add account" inside the dialog,
  // surface the click to the parent so it can open the wizard
  // pre-scoped to this provider.
  onAddAccount?: (provider: ProviderConfig) => void;
  // `{account_id: status}` for accounts whose credential is expired or rejected, so the
  // key/cookie badges stop reading as green. Best-effort: absent means "no known problem".
  credentialProblems?: Record<string, string>;
}

export function ProviderDetailDialog({
  provider,
  connections,
  onSelectConnection,
  onClose,
  onAccountDeleted,
  onAddAccount,
  credentialProblems,
}: ProviderDetailDialogProps) {
  const [editingAccount, setEditingAccount] = useState<ProviderAccount | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ProviderAccount | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [editingDiscovered, setEditingDiscovered] = useState<ProviderAccount | null>(null);
  const [discoveredLabel, setDiscoveredLabel] = useState('');
  // The master toggle's scope: which accounts the switch speaks for. The
  // description carries it, and the switch points at it with
  // aria-describedby so the qualifier reaches a screen reader too.
  const masterToggleScopeId = useId();

  // Master enabled toggle: enables/disables every account under this provider
  // via N PUTs (one per account). For v1 the backend has no batch endpoint;
  // each toggle is an admin mutation that lands in audit_log. For users with
  // many accounts this is slow but functionally correct.
  //
  // Known debt (Hermes review on PR #294): the `Promise.all` here leaves
  // accounts split on a mid-batch failure — e.g. 4 of 5 PUTs land before
  // the 5th returns a 4xx. The toast surfaces the raw API error but does
  // not roll back the partial state. Invalidate runs in `onSettled` so a
  // partial failure still refetches true DB state; full per-account
  // rollback (or a server-side batch endpoint) is left for a follow-up.
  const queryClient = useQueryClient();
  // Discovered rows have no provider_configs row — PUT would *create* one
  // (persisting an opaque hash), and Remove has nothing to delete. Master
  // toggle therefore only drives config-backed accounts; discovered rows
  // are read-only "auto" entries (Hermes review on PR #309).
  //
  // Archived rows are hidden entirely (Hermes review on PR #317 round-2 re-review):
  // the Remove action soft-archives server-side, and leaving the row
  // visible as "disabled" with a live Edit/Remove menu contradicts the
  // confirm copy. They're also excluded from the master toggle so a
  // bulk enable can't resurrect them — the server-side invariant
  // (archived ⇒ enabled=False) is the hard stop; this is the UI layer.
  // The Claude family groups several providers under one card, and that card
  // counts every account in the family. The dialog therefore lists every
  // account in the family too — each row says which connection it belongs to
  // — instead of only the active connection's. An account the server
  // discovered for itself was invisible here until now: it could not be
  // renamed, disabled, or priced (#723).
  //
  // Every account carries the provider it belongs to, so an account is always
  // addressed to that provider — never to the family's representative.
  const familyAccounts = useMemo(() => {
    if (!connections || connections.length < 2) return null;
    return connections.flatMap((connection) =>
      (connection.accounts ?? []).map((account) => ({
        ...account,
        provider_id: account.provider_id ?? connection.provider_id,
      })),
    );
  }, [connections]);
  const listedAccounts = (familyAccounts ?? provider?.accounts ?? []).filter((a) => !a.archived);
  const configAccounts = listedAccounts.filter((a) => a.source !== 'discovered');

  // The provider an account is addressed to: its own, not the family's
  // representative. An account that names a provider the dialog does not
  // know is left unaddressed rather than filed under the family's
  // representative — a wrong address is the very thing this fix is about.
  const providerOf = (account: ProviderAccount): ProviderConfig | null => {
    const id = account.provider_id;
    if (!id || !provider) return provider;
    if (id === provider.provider_id) return provider;
    return (connections ?? []).find((c) => c.provider_id === id) ?? null;
  };
  // The provider the account dialog opens against — its own, or nothing. There
  // is no fallback to the family's representative: the account mutations already
  // refuse to act on an account they cannot address, and this dialog has to
  // agree with them rather than show the wrong provider's settings.
  const editingAccountProvider = editingAccount ? providerOf(editingAccount) : null;
  // The accounts the master toggle can actually reach. An account the dialog
  // cannot address is left out of the blast radius rather than silently
  // dropped from it, and the switch says so when none of them is reachable.
  const addressableConfigAccounts = configAccounts.filter((a) => providerOf(a) !== null);
  const masterEnabled =
    addressableConfigAccounts.length > 0 && addressableConfigAccounts.every((a) => a.enabled);
  const setMasterEnabled = useMutation({
    mutationFn: async (next: boolean) => {
      const skipped: string[] = [];
      const updates = configAccounts
        .filter((a) => a.enabled !== next)
        .flatMap((a) => {
          const target = providerOf(a);
          if (!target) {
            // An account the dialog cannot address is left alone — and named,
            // so the toast can never claim a family-wide change that did not
            // happen. The name goes through the display helper: the id may be
            // an opaque 64-hex credential hash, and every other toast here
            // masks it.
            skipped.push(displayAccountName(a));
            return [];
          }
          return [putProviderConfig(target.provider_id, a.account_id, { enabled: next })];
        });
      await Promise.all(updates);
      return { changed: updates.length, skipped };
    },
    onSuccess: ({ changed, skipped }, next) => {
      // Capture `next` from the mutation variables — by the time onSuccess
      // fires, the parent state may have updated and `masterEnabled` reflects
      // the post-toggle value, not the user's intent. (Variable form of
      // useMutation's onSuccess callback.)
      const verb = next ? 'enabled' : 'disabled';
      const name = provider?.name;
      if (skipped.length > 0) {
        // Not a success: part of the family was left alone, and the operator
        // has to hear that from the one report they get. "N changed; M
        // skipped" says what happened — "N of T accounts" mixes the two and
        // under-reports when some accounts were already in the target state.
        toast.error(
          `${name} · ${changed} ${changed === 1 ? 'account' : 'accounts'} ${verb}; ` +
            `${skipped.length} skipped (${skipped.join(', ')})`,
        );
        return;
      }
      toast.success(`${name} · all accounts ${verb} (${changed} changed)`);
    },
    onError: (err) => toast.error(err.message),
    // Refetch on success *and* failure so a mid-batch partial failure still
    // shows the true DB state instead of the pre-toggle snapshot.
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
    },
  });

  const archiveAccount = useMutation({
    mutationFn: (account: ProviderAccount) => {
      const target = providerOf(account);
      if (!target) throw new Error('no provider');
      return putProviderConfig(target.provider_id, account.account_id, {
        archived: true,
      });
    },
    onSuccess: (_data, account) => {
      toast.success(`${provider?.name} · ${maskAccountId(account.account_id)} archived`);
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
      setMenuFor(null);
    },
    onError: (err) => toast.error(err.message),
  });

  const remove = useMutation({
    mutationFn: (account: ProviderAccount) => {
      const target = providerOf(account);
      if (!target) throw new Error('no provider');
      return deleteProviderConfig(target.provider_id, account.account_id);
    },
    onSuccess: (_data, account) => {
      toast.success(`${provider?.name} · ${maskAccountId(account.account_id)} removed`);
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      onAccountDeleted?.(provider?.provider_id ?? '', account.account_id);
      setPendingDelete(null);
      setMenuFor(null);
    },
    onError: (err) => toast.error(err.message),
  });
  const saveDiscoveredLabel = useMutation({
    mutationFn: () => {
      if (!editingDiscovered) throw new Error('No discovered account selected');
      const target = providerOf(editingDiscovered);
      if (!target) throw new Error('no provider');
      return putDiscoveredAccountLabel(target.provider_id, editingDiscovered.account_id, discoveredLabel.trim() || null);
    },
    onSuccess: () => {
      toast.success('Account label saved');
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      setEditingDiscovered(null);
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <>
      <ResponsiveDialog
        open={provider !== null}
        onOpenChange={(open) => {
          if (!open) {
            setEditingAccount(null);
            setPendingDelete(null);
            setMenuFor(null);
            onClose();
          }
        }}
        title={connections && connections.length > 1 ? 'Claude' : (provider?.name ?? '')}
        description={
          provider
            ? listedAccounts.length === 0
              ? 'Not configured'
              : `${listedAccounts.length} ${listedAccounts.length === 1 ? 'account' : 'accounts'} · poll ${
                  provider.effective_poll_interval ?? provider.default_ttl_seconds ?? '—'
                }s`
            : ''
        }
        width="max-w-xl"
      >
        {provider ? (
          <div className="flex flex-col gap-3">
            <ProviderDetailHeader
              provider={provider}
              connections={connections}
              onSelectConnection={onSelectConnection}
              masterToggleScopeId={masterToggleScopeId}
              masterEnabled={masterEnabled}
              addressableConfigAccounts={addressableConfigAccounts}
              setMasterEnabled={setMasterEnabled}
            />
            <ProviderDetailAccountList
              provider={provider}
              connections={connections}
              listedAccounts={listedAccounts}
              credentialProblems={credentialProblems}
              menuFor={menuFor}
              setMenuFor={setMenuFor}
              setEditingDiscovered={setEditingDiscovered}
              setDiscoveredLabel={setDiscoveredLabel}
              providerOf={providerOf}
              setEditingAccount={setEditingAccount}
              archiveAccount={archiveAccount}
              setPendingDelete={setPendingDelete}
              onAddAccount={onAddAccount}
            />
            {provider.provider_id === 'anthropic' ? <ClaudeCloudCollectorSwitch /> : null}

            {pendingDelete ? (
              <div className="rounded-md border border-critical/30 bg-critical/5 p-3">
                <p className="mb-2 text-[12px] text-critical">
                  Remove <strong>{displayAccountName(pendingDelete)}</strong> (
                  <code className="font-mono text-[11px]">
                    {maskAccountId(pendingDelete.account_id)}
                  </code>
                  )?
                  This archives the account and clears its stored credentials. It will
                  disappear from Settings.
                </p>
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" size="sm" onClick={() => setPendingDelete(null)}>
                    Cancel
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => remove.mutate(pendingDelete)}
                    loading={remove.isPending}
                  >
                    Remove account
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </ResponsiveDialog>

      {provider && editingAccount && editingAccountProvider ? (
        <ProviderAccountDialog
          provider={editingAccountProvider}
          accountId={editingAccount.account_id}
          onClose={() => setEditingAccount(null)}
        />
      ) : null}
      <ResponsiveDialog
        open={editingDiscovered !== null}
        onOpenChange={(open) => { if (!open) setEditingDiscovered(null); }}
        title="Edit discovered account label"
        description="This changes the display name only. Collection continues to use the discovered credential."
      >
        <form className="flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); saveDiscoveredLabel.mutate(); }}>
          <label className="flex flex-col gap-1 text-[12px] text-fg-muted">
            Display label
            <input autoFocus maxLength={120} value={discoveredLabel} onChange={(event) => setDiscoveredLabel(event.target.value)} className="rounded border border-edge bg-surface-1 px-2 py-1.5 text-fg" />
          </label>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => setEditingDiscovered(null)}>Cancel</Button>
            <Button type="submit" size="sm" loading={saveDiscoveredLabel.isPending}>Save label</Button>
          </div>
        </form>
      </ResponsiveDialog>
    </>
  );
}
