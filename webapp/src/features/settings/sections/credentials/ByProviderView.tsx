// Provider → account → the credentials behind it, with the answer to "where is this
// account's data actually coming from".

import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { ClipboardCopy, FlaskConical } from 'lucide-react';
import { toast } from 'sonner';
import { probeCredentialSources } from '@/api/endpoints';
import type { CredentialAccountView, CredentialProviderView, SourceProbeResult } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { copyText } from '@/lib/clipboard';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { Tooltip } from '@/components/ui/Tooltip';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { displayAccountName, accountSubtitle } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';
import { STATUS_VARIANT, STATUS_LABEL, diagnosticsText, originSummary } from './display';
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

function AccountBlock({
  account,
  providerName,
}: {
  account: CredentialAccountView;
  providerName: string;
}) {
  const subtitle = accountSubtitle(account);
  const from = dataFrom(account);
  const [fallbackText, setFallbackText] = useState<string | null>(null);

  // A live test of every source of this account. It makes real upstream requests but writes
  // nothing (no health, no refresh), so it is safe to run on a dead-looking credential.
  const retest = useMutation({
    mutationFn: () => probeCredentialSources(account.provider_id, account.account_id),
    onError: (err: Error) =>
      toast.error(
        /429|too many/i.test(err.message)
          ? 'Re-test is rate-limited; try again in a minute'
          : `Re-test failed: ${err.message}`,
      ),
  });
  const probes = new Map<string, SourceProbeResult>(
    (retest.data?.sources ?? []).map((r) => [r.source_id, r]),
  );

  const copyDiagnostics = async () => {
    const text = diagnosticsText(providerName, account);
    if (await copyText(text)) toast.success('Diagnostics copied');
    else setFallbackText(text);
  };
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
        <span className="ml-auto flex items-center gap-1">
          {!account.identity_pending ? (
            <Button
              variant="ghost"
              size="sm"
              loading={retest.isPending}
              aria-label={`Re-test ${accountName(account)}`}
              onClick={() => retest.mutate()}
            >
              <FlaskConical className="size-3.5" aria-hidden />
              Re-test
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`Copy diagnostics for ${accountName(account)}`}
            onClick={copyDiagnostics}
          >
            <ClipboardCopy className="size-3.5" aria-hidden />
          </Button>
        </span>
      </div>
      {from.via ? (
        <Tooltip content={`Collection path: ${from.via}`}>
          <p className="mt-0.5 text-[11px] text-fg-muted">{from.text}</p>
        </Tooltip>
      ) : (
        <p className="mt-0.5 text-[11px] text-fg-muted">{from.text}</p>
      )}
      <SourceList
        sources={account.sources}
        label={`${accountName(account)} credentials`}
        providerName={providerName}
        account={{ provider_id: account.provider_id, account_id: account.account_id }}
        probes={probes}
        probed={retest.isSuccess}
      />
      <ResponsiveDialog
        open={fallbackText !== null}
        onOpenChange={(open) => !open && setFallbackText(null)}
        title="Copy diagnostics"
        description="Your browser blocked clipboard access. Select the text and copy it manually."
      >
        <textarea
          readOnly
          autoFocus
          aria-label="Diagnostics"
          className="h-48 w-full rounded-md border border-edge bg-surface-1 p-2 font-mono text-[11px]"
          value={fallbackText ?? ''}
          onFocus={(e) => e.currentTarget.select()}
        />
      </ResponsiveDialog>
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
              <AccountBlock key={`${a.provider_id}/${a.account_id}`} account={a} providerName={p.name} />
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
}
