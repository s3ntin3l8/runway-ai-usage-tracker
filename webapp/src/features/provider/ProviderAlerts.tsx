// Surfaces anything worth a glance at the top of Overview: per-model usage
// spikes (z-score anomalies) and recent provider errors. Renders nothing when
// there is nothing to report.

import { AlertTriangle, CircleAlert } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { formatTokens } from '@/lib/format';
import { useProviderAnomalies, useProviderErrors } from './queries';

export function ProviderAlerts({
  providerId,
  accountId,
}: {
  providerId: string;
  accountId: string;
}) {
  const anomalies = useProviderAnomalies(providerId, accountId);
  const errors = useProviderErrors(providerId, accountId);

  const spikes = anomalies.data?.anomalies ?? [];
  const errorEvents = errors.data?.events ?? [];

  if (spikes.length === 0 && errorEvents.length === 0) return null;

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
      {spikes.length > 0 ? (
        <Card className="flex items-start gap-2.5 bg-warning-muted px-4 py-2.5 text-[13px]">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          <span className="text-fg">
            Usage spike on{' '}
            <span className="font-medium">
              {spikes
                .slice(0, 3)
                .map((s) => s.model_id)
                .join(', ')}
            </span>
            {spikes.length > 3 ? ` +${spikes.length - 3} more` : ''}
            <span className="text-fg-muted">
              {' '}
              — {formatTokens(spikes[0].today_tokens)} today vs{' '}
              {formatTokens(spikes[0].historical_mean_tokens)} avg (
              {spikes[0].z_score_tokens.toFixed(1)}σ)
            </span>
          </span>
        </Card>
      ) : null}
    </div>
  );
}
