import type { CredentialAccountView, CredentialSourceView } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { STATUS_LABEL, STATUS_VARIANT, originSummary } from '@/features/settings/sections/credentials/display';
import { labelOrMaskedId } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';

export function CredentialSourceSummary({
  account,
  source,
}: {
  account: CredentialAccountView;
  source: CredentialSourceView;
}) {
  return (
    <div className="mt-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[13px]">{originSummary(source)}</span>
        <Badge variant={STATUS_VARIANT[source.status] ?? 'neutral'}>
          {STATUS_LABEL[source.status] ?? source.status}
        </Badge>
      </div>
      <p className="text-[11px] text-fg-subtle">
        {labelOrMaskedId({ account_id: account.account_id, account_label: account.account_label })}
        {source.last_success_at ? ` · collected ${timeAgo(source.last_success_at)}` : ''}
      </p>
    </div>
  );
}
