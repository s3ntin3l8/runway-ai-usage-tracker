// Per-account edit form for credentials, labels, poll intervals, and
// collection strategies. It is keyed by `(provider_id, account_id)` and
// opened from `ProviderDetailDialog` for the selected account.

import { useMemo } from 'react';
import type { ProviderConfig } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { displayAccountName, accountSubtitle, maskAccountId } from '@/lib/accountDisplay';
import { ProviderAccountForm } from './providerAccount/ProviderAccountForm';

interface ProviderAccountDialogProps {
  // The provider envelope — passed for label/help text and supported_strategies
  // defaults. `provider.accounts[i]` is the account being edited.
  provider: ProviderConfig;
  accountId: string;
  onClose: () => void;
  onSaved?: () => void;
}

export function ProviderAccountDialog({
  provider,
  accountId,
  onClose,
  onSaved,
}: ProviderAccountDialogProps) {
  const account = useMemo(
    () => provider.accounts.find((a) => a.account_id === accountId),
    [provider.accounts, accountId],
  );
  const title = account
    ? `Edit account · ${displayAccountName(account)}`
    : `Edit account · ${maskAccountId(accountId)}`;

  if (!account) {
    // Defensive: render an empty-state dialog rather than crashing if the
    // account was deleted between the parent state update and this render.
    return (
      <ResponsiveDialog
        open
        onOpenChange={(open) => {
          if (!open) onClose();
        }}
        title="Account not found"
        description="This account may have been removed."
      >
        <Button variant="secondary" onClick={onClose}>
          Close
        </Button>
      </ResponsiveDialog>
    );
  }

  return (
    <ResponsiveDialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={title}
      description={
        accountSubtitle(account)
          ? `${accountSubtitle(account)} · stored encrypted on the server`
          : 'Stored encrypted on the server'
      }
    >
      <ProviderAccountForm
        key={account.account_id}
        provider={provider}
        account={account}
        onSaved={() => {
          onSaved?.();
          onClose();
        }}
        onCancel={onClose}
      />
    </ResponsiveDialog>
  );
}
