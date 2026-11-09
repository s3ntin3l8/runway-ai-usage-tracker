import type { LimitCard } from '@/api/types';
import { formatCurrency } from '@/lib/format';

/** Muted line under an allowance gauge: spend that no tracked usage event explains. */
export function UntrackedSpendNote({ card }: { card: Pick<LimitCard, 'untracked_spend_usd' | 'currency'> }) {
  const value = card.untracked_spend_usd;
  if (value == null) return null;
  return (
    <p className="mt-1.5 text-[11px] text-fg-muted">
      {formatCurrency(value, card.currency ?? 'USD')} of this period&apos;s spend is not from tracked clients
    </p>
  );
}
