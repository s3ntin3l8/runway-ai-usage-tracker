// Shared "Cost outlook" card: MTD spend → projected EOM gauge + 7-day burn.
// Extracted from ForecastTab so the quota Forecast tab, the spend Forecast tab,
// and the spend Overview hero all share one component.

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Gauge } from '@/components/ui/Gauge';
import { Skeleton } from '@/components/ui/Skeleton';
import { formatCost } from '@/lib/format';
import { statusForPct } from '@/lib/quota';
import { useProviderCostForecast } from './queries';

export function CostOutlookCard({
  providerId,
  accountId,
  className,
}: {
  providerId: string;
  accountId: string;
  className?: string;
}) {
  const cost = useProviderCostForecast(providerId, accountId);

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>Cost outlook</CardTitle>
        {cost.data ? (
          <span className="text-[11px] text-fg-subtle">{cost.data.days_remaining}d left</span>
        ) : null}
      </CardHeader>
      <CardContent>
        {cost.isPending ? (
          <Skeleton className="h-20 w-full" />
        ) : cost.data ? (
          <>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[13px] text-fg-muted">
                <strong className="font-mono tabular text-fg">
                  {formatCost(cost.data.current_month_to_date)}
                </strong>{' '}
                spent
              </span>
              <span className="text-[13px] text-fg-muted">
                →{' '}
                <strong className="font-mono tabular text-fg">
                  {formatCost(cost.data.projected_eom)}
                </strong>{' '}
                projected
              </span>
            </div>
            <Gauge
              pct={
                cost.data.projected_eom > 0
                  ? (cost.data.current_month_to_date / cost.data.projected_eom) * 100
                  : 0
              }
              status={statusForPct(
                cost.data.projected_eom > 0
                  ? (cost.data.current_month_to_date / cost.data.projected_eom) * 100
                  : 0,
              )}
              className="mt-2"
            />
            <p className="mt-2 text-[11px] text-fg-subtle">
              {formatCost(cost.data.daily_burn_avg_7d)}/day average over the last 7 days.
            </p>
          </>
        ) : (
          <p className="py-6 text-center text-xs text-fg-subtle">No cost data yet.</p>
        )}
      </CardContent>
    </Card>
  );
}
