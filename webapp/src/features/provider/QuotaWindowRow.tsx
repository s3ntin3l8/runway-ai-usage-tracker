// One quota-window row for the Overview "Quota windows" card: label + pct, a
// gauge with a glide-path marker, and a foot that surfaces the forecast
// (projected % by reset, or run-out time when on track to exhaust) plus a
// pacing verdict. Ports the glide/forecast logic from the v1 fleet-commander,
// consuming the backend's forecast fields instead of recomputing client-side.

import type { ForecastEntry, ForecastStatus, LimitCard } from '@/api/types';
import { Countdown } from '@/components/ui/Countdown';
import { Gauge } from '@/components/ui/Gauge';
import { cn } from '@/lib/cn';
import { formatPct } from '@/lib/format';
import { cardPct, cardStatus, chipLabel, windowLabel } from '@/lib/quota';
import { formatLocalDateTime } from '@/lib/tz';

function statusColor(status: ForecastStatus | undefined): string {
  if (status === 'risk' || status === 'exhausted') return 'text-critical';
  if (status === 'warn' || status === 'near_limit') return 'text-warning';
  return 'text-fg-subtle';
}

export function QuotaWindowRow({
  card,
  siblings,
  forecast,
}: {
  card: LimitCard;
  siblings: LimitCard[];
  forecast: ForecastEntry | null;
}) {
  const used = cardPct(card);
  const glide = forecast?.glide_pct ?? null;

  // Forecast summary (right of the reset countdown).
  let forecastNode;
  if (
    forecast &&
    (forecast.status === 'risk' || forecast.status === 'exhausted') &&
    forecast.projected_limit_hit_at
  ) {
    forecastNode = (
      <span className="font-medium text-critical">
        runs out{' '}
        {formatLocalDateTime(forecast.projected_limit_hit_at, {
          weekday: 'short',
          hour: '2-digit',
          minute: '2-digit',
        })}
      </span>
    );
  } else if (forecast?.projected_pct != null) {
    forecastNode = (
      <span className={cn('font-medium', statusColor(forecast.status))}>
        → {Math.round(forecast.projected_pct)}% by reset
      </span>
    );
  } else {
    forecastNode = <span className="text-fg-subtle">{windowLabel(card) ?? card.window_type}</span>;
  }

  // Pacing verdict from glide_pct vs current usage.
  let pace: string | null = null;
  if (glide != null && used != null) {
    if (used > glide + 4) pace = `↑ ${Math.round(used - glide)}% ahead of pace`;
    else if (used < glide - 4) pace = `${Math.round(glide - used)}% under pace`;
    else pace = 'on glide path';
  }

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[13px] font-medium">{chipLabel(card, siblings)}</span>
        <span className="font-mono text-[13px] font-semibold tabular">
          {used != null ? formatPct(used) : (card.remaining ?? '—')}
        </span>
      </div>
      <Gauge pct={used} status={cardStatus(card)} glide={glide} size="xl" className="mt-1.5" />
      <div className="mt-1.5 flex items-center justify-between gap-2 text-[11px]">
        <Countdown until={card.reset_at} className="text-[11px]" />
        <span className="text-center text-fg-subtle">{pace ?? ''}</span>
        {forecastNode}
      </div>
    </div>
  );
}
