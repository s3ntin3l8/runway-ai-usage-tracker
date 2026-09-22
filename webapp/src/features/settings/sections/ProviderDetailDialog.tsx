// Per-provider detail dialog. Shows the master enabled toggle + a vertical
// list of account rows for one ProviderConfig. Each row exposes a "⋮" menu
// with Edit / Remove actions. The "Add account" footer button is wired in
// #287 (wizard lands); #286 ships it disabled with a tooltip pointing at
// the follow-up PR.
//
// Lives behind the v2 settings UI shell (`?providers=v2`).

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { MoreHorizontal, Pencil, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { deleteProviderConfig, putProviderConfig } from '@/api/endpoints';
import type { ProviderAccount, ProviderConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Switch } from '@/components/ui/Switch';
import { displayAccountName, accountSubtitle } from '@/lib/accountDisplay';
import { ProviderAccountDialog } from './ProviderAccountDialog';

interface ProviderDetailDialogProps {
  provider: ProviderConfig | null;
  onClose: () => void;
  onAccountDeleted?: (providerId: string, accountId: string) => void;
}

export function ProviderDetailDialog({
  provider,
  onClose,
  onAccountDeleted,
}: ProviderDetailDialogProps) {
  const [editingAccount, setEditingAccount] = useState<ProviderAccount | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ProviderAccount | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);

  // Master enabled toggle: enables/disables every account under this provider
  // via N PUTs (one per account). For v1 the backend has no batch endpoint;
  // each toggle is an admin mutation that lands in audit_log. For users with
  // many accounts this is slow but functionally correct.
  //
  // Known debt (Hermes review on PR #294): the `Promise.all` here leaves
  // accounts split on a mid-batch failure — e.g. 4 of 5 PUTs land before
  // the 5th returns a 4xx. The toast surfaces the raw API error but does
  // not roll back the partial state, so the UI shows a mix of enabled and
  // disabled rows until the next refetch. Adding per-account rollback (or a
  // server-side batch endpoint) is the durable fix; left for a follow-up
  // so v1 can ship.
  const queryClient = useQueryClient();
  const masterEnabled =
    provider !== null && provider.accounts.length > 0 && provider.accounts.every((a) => a.enabled);

  const setMasterEnabled = useMutation({
    mutationFn: async (next: boolean) => {
      if (!provider) return;
      const updates = provider.accounts
        .filter((a) => a.enabled !== next)
        .map((a) =>
          putProviderConfig(provider.provider_id, a.account_id, { enabled: next }),
        );
      await Promise.all(updates);
    },
    onSuccess: (_data, next) => {
      // Capture `next` from the mutation variables — by the time onSuccess
      // fires, the parent state may have updated and `masterEnabled` reflects
      // the post-toggle value, not the user's intent. (Variable form of
      // useMutation's onSuccess callback.)
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      if (provider) {
        toast.success(
          provider.accounts.length === 0
            ? 'No accounts to update'
            : `${provider.name} · all accounts ${next ? 'enabled' : 'disabled'}`,
        );
      }
    },
    onError: (err) => toast.error(err.message),
  });

  const remove = useMutation({
    mutationFn: (accountId: string) => {
      if (!provider) throw new Error('no provider');
      return deleteProviderConfig(provider.provider_id, accountId);
    },
    onSuccess: (_data, accountId) => {
      toast.success(`${provider?.name} · ${accountId} removed`);
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      onAccountDeleted?.(provider?.provider_id ?? '', accountId);
      setPendingDelete(null);
      setMenuFor(null);
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
        title={provider?.name ?? ''}
        description={
          provider
            ? provider.account_count === 0
              ? 'Not configured'
              : `${provider.account_count} ${provider.account_count === 1 ? 'account' : 'accounts'} · poll ${
                  provider.effective_poll_interval ?? provider.default_ttl_seconds ?? '—'
                }s`
            : ''
        }
        width="max-w-xl"
      >
        {provider ? (
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between rounded-sm border border-edge bg-surface-2 px-3 py-2">
              <div className="flex items-center gap-2">
                <ProviderGlyph providerId={provider.provider_id} name={provider.name} />
                <div>
                  <p className="text-[13px] font-medium">All accounts enabled</p>
                  <p className="text-[11px] text-fg-subtle">
                    Disabling hides this provider from collection across all accounts.
                  </p>
                </div>
              </div>
              <Switch
                checked={masterEnabled}
                disabled={provider.accounts.length === 0 || setMasterEnabled.isPending}
                onCheckedChange={(v) => setMasterEnabled.mutate(v)}
                aria-label="All accounts enabled"
              />
            </div>

            {provider.accounts.length === 0 ? (
              <EmptyState
                title="No accounts configured"
                description="Add a credential to start collecting usage for this provider."
                action={
                  <Button variant="primary" size="sm" disabled aria-disabled="true" title="Wizard lands in #287">
                    Add account
                  </Button>
                }
              />
            ) : (
              <ul className="flex flex-col gap-1.5" aria-label="Provider accounts">
                {provider.accounts.map((account) => {
                  const isMenuOpen = menuFor === account.account_id;
                  return (
                    <li
                      key={account.account_id}
                      className="flex items-center gap-3 rounded-sm border border-edge bg-surface-1 px-3 py-2.5"
                    >
                      <AccountInitial account={account} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[13px] font-medium">
                          {displayAccountName(account)}
                        </p>
                        {accountSubtitle(account) ? (
                          <p className="truncate text-[11px] text-fg-subtle">
                            {accountSubtitle(account)}
                          </p>
                        ) : null}
                        {account.is_orphaned ? (
                          <p className="mt-0.5 text-[11px] text-warning">
                            No usage data — safe to remove
                          </p>
                        ) : null}
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5">
                        {account.api_key_set ? <Badge variant="ok">key</Badge> : null}
                        {account.session_cookie_set ? (
                          <Badge variant="ok">cookie</Badge>
                        ) : null}
                        <Badge variant={account.enabled ? 'accent' : 'neutral'}>
                          {account.enabled ? 'enabled' : 'disabled'}
                        </Badge>
                      </div>
                      <div className="relative">
                        <Button
                          size="icon-sm"
                          variant="ghost"
                          aria-label={`Actions for ${displayAccountName(account)}`}
                          aria-haspopup="menu"
                          aria-expanded={isMenuOpen}
                          onClick={() =>
                            setMenuFor((current) =>
                              current === account.account_id ? null : account.account_id,
                            )
                          }
                        >
                          <MoreHorizontal className="size-4" />
                        </Button>
                        {isMenuOpen ? (
                          <div
                            role="menu"
                            className="absolute top-full right-0 z-10 mt-1 min-w-[10rem] rounded-md border border-edge bg-overlay p-1 shadow-lg"
                            onMouseLeave={() => setMenuFor(null)}
                          >
                            <button
                              type="button"
                              role="menuitem"
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[13px] hover:bg-surface-2"
                              onClick={() => {
                                setMenuFor(null);
                                setEditingAccount(account);
                              }}
                            >
                              <Pencil className="size-3.5 text-fg-muted" />
                              Edit
                            </button>
                            <button
                              type="button"
                              role="menuitem"
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[13px] text-critical hover:bg-critical/10"
                              onClick={() => {
                                setMenuFor(null);
                                setPendingDelete(account);
                              }}
                            >
                              <Trash2 className="size-3.5" />
                              Remove
                            </button>
                          </div>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}

            {provider.accounts.length > 0 ? (
              <Button
                variant="secondary"
                size="sm"
                className="self-start"
                disabled
                aria-disabled="true"
                title="Wizard lands in #287"
              >
                Add account
              </Button>
            ) : null}

            {pendingDelete ? (
              <div className="rounded-md border border-critical/30 bg-critical/5 p-3">
                <p className="mb-2 text-[12px] text-critical">
                  Remove <strong>{displayAccountName(pendingDelete)}</strong> (
                  <code className="font-mono text-[11px]">{pendingDelete.account_id}</code>)?
                  This deletes the configuration row and its stored credentials.
                </p>
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" size="sm" onClick={() => setPendingDelete(null)}>
                    Cancel
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => remove.mutate(pendingDelete.account_id)}
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

      {provider && editingAccount ? (
        <ProviderAccountDialog
          provider={provider}
          accountId={editingAccount.account_id}
          onClose={() => setEditingAccount(null)}
        />
      ) : null}
    </>
  );
}

function AccountInitial({ account }: { account: ProviderAccount }) {
  // Simple visual initial derived from the account_label / account_id. Keeps
  // the row scannable without dragging in the full ProviderGlyph.
  const source = account.account_label?.trim() || account.account_id;
  const ch = (source[0] ?? '?').toUpperCase();
  return (
    <div
      aria-hidden
      className="flex size-7 shrink-0 items-center justify-center rounded-sm bg-accent-muted text-[11px] font-semibold text-accent"
    >
      {ch}
    </div>
  );
}
