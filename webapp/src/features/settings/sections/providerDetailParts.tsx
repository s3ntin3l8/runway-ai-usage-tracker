// Shared by the provider detail account row. A component and a plain helper
// live together so both move as one sibling of ProviderDetailDialog.
/* eslint-disable react-refresh/only-export-components */

import type { ProviderAccount } from '@/api/types';

export function credentialProblemTitle(status: string | undefined): string | undefined {
  return status ? `A credential is ${status} — see Credentials` : undefined;
}

export function AccountInitial({ account }: { account: ProviderAccount }) {
  // Simple visual initial derived from the account_label / account_id. Keeps
  // the row scannable without dragging in the full ProviderGlyph.
  const source = account.account_label?.trim() || account.account_id;
  const ch = (source[0] ?? '?').toUpperCase();
  return (
    <div
      aria-hidden
      className="flex size-7 shrink-0 items-center justify-center rounded-sm bg-accent-muted text-[11px] font-semibold text-accent-text"
    >
      {ch}
    </div>
  );
}
