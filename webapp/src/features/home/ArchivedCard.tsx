// Compact read-only card for archived providers — shows lifetime stats
// (tokens, msgs, cost, last activity) in a non-interactive format.

import { useNavigate } from 'react-router';
import { Card } from '@/components/ui/Card';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { formatCurrency, formatNumber, formatTokens, timeAgo } from '@/lib/format';
import type { ArchivedProvider } from '@/api/endpoints';

export function ArchivedCard({
  item,
  providerName,
}: {
  item: ArchivedProvider;
  providerName: string;
}) {
  const navigate = useNavigate();
  const life = item.lifetime;

  const tokensTotal = life
    ? (life.tokens_input ?? 0) + (life.tokens_output ?? 0) + (life.tokens_cache_read ?? 0) + (life.tokens_cache_create ?? 0) + (life.tokens_reasoning ?? 0)
    : null;

  return (
    <Card
      role="button"
      tabIndex={0}
      onClick={() => navigate(`/provider/${item.provider_id}?account=${item.account_id}`)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate(`/provider/${item.provider_id}?account=${item.account_id}`);
        }
      }}
      className="flex min-h-36 cursor-pointer flex-col p-3.5 transition-colors duration-150 hover:border-edge-strong opacity-70 hover:opacity-100"
    >
      <div className="flex items-center gap-2.5">
        <ProviderGlyph providerId={item.provider_id} name={providerName} className="size-6 text-[10px]" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium">{providerName}</p>
          {item.account_id && item.account_id !== 'default' ? (
            <p className="truncate text-[11px] text-fg-subtle">{item.account_id}</p>
          ) : null}
        </div>
        <span className="shrink-0 rounded bg-surface-2 px-1.5 py-0.5 text-[10px] font-medium text-fg-muted">
          archived
        </span>
      </div>

      <div className="mt-auto pt-3">
        {life ? (
          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <div>
              <p className="text-fg-muted">Tokens</p>
              <p className="font-mono font-medium tabular">{formatTokens(tokensTotal)}</p>
            </div>
            <div>
              <p className="text-fg-muted">Msgs</p>
              <p className="font-mono font-medium tabular">{formatNumber(life.msgs)}</p>
            </div>
            <div>
              <p className="text-fg-muted">Cost</p>
              <p className="font-mono font-medium tabular">{formatCurrency(life.cost_usd)}</p>
            </div>
          </div>
        ) : (
          <p className="text-[11px] text-fg-muted">No usage data</p>
        )}

        {item.last_activity_ts ? (
          <p className="mt-2 text-[10px] text-fg-subtle">
            Last active {timeAgo(item.last_activity_ts)}
          </p>
        ) : null}
      </div>
    </Card>
  );
}
