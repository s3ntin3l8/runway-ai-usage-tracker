// One account row in the provider detail dialog: identity, credential badges,
// and the Edit / Archive / Remove menu.

import type { Dispatch, SetStateAction } from 'react';
import { Archive, Ellipsis, Pencil, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import type { ProviderAccount, ProviderConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { accountSubtitle, displayAccountName } from '@/lib/accountDisplay';
import { claudeConnectionLabel } from '@/lib/providerFamilies';
import { AccountInitial } from './AccountInitial';
import { credentialProblemTitle } from './credentialProblemTitle';

export interface ProviderDetailAccountDetailProps {
  account: ProviderAccount;
  provider: ProviderConfig;
  connections?: ProviderConfig[];
  credentialProblems?: Record<string, string>;
  menuFor: string | null;
  setMenuFor: Dispatch<SetStateAction<string | null>>;
  setEditingDiscovered: Dispatch<SetStateAction<ProviderAccount | null>>;
  setDiscoveredLabel: Dispatch<SetStateAction<string>>;
  providerOf: (account: ProviderAccount) => ProviderConfig | null;
  setEditingAccount: Dispatch<SetStateAction<ProviderAccount | null>>;
  archiveAccount: {
    isPending: boolean;
    mutate: (account: ProviderAccount) => void;
  };
  setPendingDelete: Dispatch<SetStateAction<ProviderAccount | null>>;
}

export function ProviderDetailAccountDetail({
  account,
  provider,
  connections,
  credentialProblems,
  menuFor,
  setMenuFor,
  setEditingDiscovered,
  setDiscoveredLabel,
  providerOf,
  setEditingAccount,
  archiveAccount,
  setPendingDelete,
}: ProviderDetailAccountDetailProps) {
  const isMenuOpen = menuFor === account.account_id;
  return (
    <li className="flex items-center gap-3 rounded-sm border border-edge bg-surface-1 px-3 py-2.5">
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
        {connections && connections.length > 1 ? (
          <p className="mt-0.5 truncate text-[11px] text-fg-subtle">
            {claudeConnectionLabel(account.provider_id ?? provider.provider_id)}
          </p>
        ) : null}
        {account.credential_source_labels?.length ? (
          <p className="mt-0.5 truncate text-[11px] text-fg-subtle">
            Discovered: {account.credential_source_labels.join(', ')}
          </p>
        ) : null}
        {account.is_orphaned ? (
          <p className="mt-0.5 text-[11px] text-warning">
            No usage data — safe to remove
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {account.api_key_set ? (
          <Badge
            variant={credentialProblems?.[account.account_id] ? 'critical' : 'ok'}
            title={credentialProblemTitle(credentialProblems?.[account.account_id])}
          >
            key
          </Badge>
        ) : null}
        {account.session_cookie_set ? (
          <Badge
            variant={credentialProblems?.[account.account_id] ? 'critical' : 'ok'}
            title={credentialProblemTitle(credentialProblems?.[account.account_id])}
          >
            cookie
          </Badge>
        ) : null}
        {account.source === 'discovered' ? (
          <Badge variant="ok">auto</Badge>
        ) : (
          <Badge variant={account.enabled ? 'accent' : 'neutral'}>
            {account.enabled ? 'enabled' : 'disabled'}
          </Badge>
        )}
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
            <Ellipsis className="size-4" />
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
                // An account the dialog cannot address has no
                // provider to open. Marked disabled for
                // assistive tech and dimmed, but still
                // clickable: a `disabled` button swallows the
                // click and says nothing, and the operator is
                // better served by being told why.
                aria-disabled={account.source !== 'discovered' && !providerOf(account)}
                className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[13px] hover:bg-surface-2 aria-disabled:opacity-50"
                onClick={() => {
                  setMenuFor(null);
                  if (account.source === 'discovered') {
                    setEditingDiscovered(account);
                    setDiscoveredLabel(account.account_label ?? '');
                  } else if (providerOf(account)) {
                    setEditingAccount(account);
                  } else {
                    // The account names a provider the dialog does
                    // not know, so there is no provider to open
                    // its settings against. Say so, rather than
                    // closing the menu and leaving the operator
                    // wondering whether the click landed.
                    toast.error(
                      `Cannot edit ${displayAccountName(account)} — its provider is not available`,
                    );
                  }
                }}
              >
                <Pencil className="size-3.5 text-fg-muted" />
                Edit
              </button>
              {account.source !== 'discovered' ? (
                <>
                  <button
                    type="button"
                    role="menuitem"
                    disabled={archiveAccount.isPending}
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[13px] hover:bg-surface-2 disabled:opacity-50"
                    onClick={() => {
                      setMenuFor(null);
                      archiveAccount.mutate(account);
                    }}
                  >
                    <Archive className="size-3.5 text-fg-muted" />
                    Archive
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
                </>
              ) : null}
            </div>
          ) : null}
      </div>
    </li>
  );
}
