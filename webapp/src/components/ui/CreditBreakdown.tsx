import type { CreditBalanceComponent } from '@/api/types';
import { formatCurrency } from '@/lib/format';
import { formatLocalDateTime } from '@/lib/tz';

const productLabels: Record<string, string> = {
  api: 'Claude API', agent_sdk: 'Agent SDK', batch: 'Batch API', console: 'Console',
  'managed-agents': 'Managed Agents', 'managed-agents-onprem': 'Managed Agents on premises',
};

export function CreditBreakdown({ entries, compact = false }: {
  entries: CreditBalanceComponent[];
  compact?: boolean;
}) {
  return (
    <div
      className="mt-2 flex flex-col gap-1 text-[11px] text-fg-muted"
      // aria-label only names an element that has a role; on a plain div it
      // is ignored. A group is what this is: a labelled set of balances.
      role="group"
      aria-label="Credit breakdown"
    >
      {entries.map((entry, i) => (
        <div key={`${entry.kind}-${i}`}>
          <div className="flex flex-wrap justify-between gap-x-2">
            <span>{entry.label}</span>
            <span className="font-mono tabular">{formatCurrency(entry.remaining_value, entry.currency)} remaining</span>
          </div>
          {entry.granted_value != null && entry.kind !== 'purchased' ? (
            <p>{formatCurrency(entry.granted_value - entry.remaining_value, entry.currency)} of {formatCurrency(entry.granted_value, entry.currency)} used</p>
          ) : null}
          {entry.expires_at ? <p>Expires {formatLocalDateTime(entry.expires_at, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })}</p> : null}
          {!compact && entry.products.length > 0 ? <p>Applies to: {entry.products.map((product) => productLabels[product] ?? product).join(', ')}</p> : null}
        </div>
      ))}
    </div>
  );
}
