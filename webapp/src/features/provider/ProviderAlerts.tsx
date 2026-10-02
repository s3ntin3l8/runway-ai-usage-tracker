// Surfaces recent provider errors at the top of Overview.

import { CircleAlert } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { useProviderErrors } from './queries';

export function ProviderAlerts({
  providerId,
  accountId,
}: {
  providerId: string;
  accountId: string;
}) {
  const errors = useProviderErrors(providerId, accountId);

  const errorEvents = errors.data?.events ?? [];

  if (errorEvents.length === 0) return null;

  // Most frequent error reason for a one-line summary.
  const reasons = new Map<string, number>();
  for (const e of errorEvents) {
    const r = (e.error_reason as string | undefined) ?? e.stop_reason ?? 'error';
    reasons.set(r, (reasons.get(r) ?? 0) + 1);
  }
  const topReason = [...reasons.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];

  return (
    <div className="flex flex-col gap-2">
      {errorEvents.length > 0 ? (
        <Card className="flex items-center gap-2.5 bg-critical-muted px-4 py-2.5 text-[13px]">
          <CircleAlert className="size-4 shrink-0 text-critical" aria-hidden />
          <span className="text-fg">
            {errorEvents.length} {errorEvents.length === 1 ? 'error' : 'errors'} in the last 24h
            {topReason ? <span className="text-fg-muted"> — most recent: {topReason}</span> : null}
          </span>
        </Card>
      ) : null}
    </div>
  );
}
