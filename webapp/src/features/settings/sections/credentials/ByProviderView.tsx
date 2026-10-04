// Provider → account → the credentials behind it, with the answer to "where is this
// account's data actually coming from".

import type { CredentialAccountView, CredentialProviderView } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { Tooltip } from '@/components/ui/Tooltip';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { displayAccountName, accountSubtitle } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';
import { STATUS_VARIANT, STATUS_LABEL, originSummary } from './display';
import { SourceList } from './SourceList';

function accountName(a: CredentialAccountView): string {
  if (a.identity_pending) return 'Needs an account';
  return displayAccountName(a);
}

/** "Data from Codex CLI · auth.json on dev-01 · collected 3m ago" */
function dataFrom(a: CredentialAccountView): { text: string; via: string } {
  const active = a.sources.find((s) => s.source_id === a.active_source_id);
  if (!active) return { text: 'No successful collection recorded yet', via: '' };
  return {
    text: `Data from ${originSummary(active)} · collected ${timeAgo(active.last_success_at)}`,
    via: [a.data_source, a.input_source].filter(Boolean).join(' / '),
  };
}

function AccountBlock({ account }: { account: CredentialAccountView }) {
  const subtitle = accountSubtitle(account);
  const from = dataFrom(account);
  return (
    <div className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[13px] font-semibold">{accountName(account)}</p>
        {subtitle ? <span className="text-[11px] text-fg-subtle">{subtitle}</span> : null}
        <Badge variant={STATUS_VARIANT[account.status] ?? 'neutral'}>
          {STATUS_LABEL[account.status] ?? account.status}
        </Badge>
        <span className="text-[11px] text-fg-subtle">
          {account.sources.length} {account.sources.length === 1 ? 'credential' : 'credentials'}
        </span>
      </div>
      {from.via ? (
        <Tooltip content={`Collection path: ${from.via}`}>
          <p className="mt-0.5 text-[11px] text-fg-muted">{from.text}</p>
        </Tooltip>
      ) : (
        <p className="mt-0.5 text-[11px] text-fg-muted">{from.text}</p>
      )}
      <SourceList sources={account.sources} label={`${accountName(account)} credentials`} />
    </div>
  );
}

export function ByProviderView({ providers }: { providers: CredentialProviderView[] }) {
  return (
    <div className="space-y-3">
      {providers.map((p) => (
        <Card key={p.provider_id} className="p-4">
          <div className="flex items-center gap-2">
            <ProviderGlyph providerId={p.provider_id} name={p.name} className="size-6" />
            <h3 className="text-sm font-semibold">{p.name}</h3>
          </div>
          <div className="mt-1 divide-y divide-edge">
            {p.accounts.map((a) => (
              <AccountBlock key={`${a.provider_id}/${a.account_id}`} account={a} />
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
}
