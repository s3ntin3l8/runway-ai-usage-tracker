// Forecast: Theil-Sen trajectory per quota window + recent closed windows.

import { useMemo, useState } from 'react';
import type { FleetEntry, ForecastEntry } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Gauge } from '@/components/ui/Gauge';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { Skeleton } from '@/components/ui/Skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { TrajectoryChart } from '@/components/charts/TrajectoryChart';
import { formatCost, formatPct, formatTokens, timeUntil } from '@/lib/format';
import { statusForPct } from '@/lib/quota';
import { formatLocalDate, formatLocalDateTime } from '@/lib/tz';
import {
  useProviderAnomalies,
  useProviderCostForecast,
  useProviderForecast,
  useWindowHistory,
} from './queries';

const STATUS_VARIANT: Record<string, 'critical' | 'warning' | 'ok' | 'neutral'> = {
  exhausted: 'critical',
  risk: 'critical',
  near_limit: 'warning',
  warn: 'warning',
  ok: 'ok',
  stable: 'ok',
  decelerating: 'ok',
  insufficient_data: 'neutral',
  low_resolution: 'neutral',
};

function forecastKey(f: ForecastEntry): string {
  return `${f.service_name ?? ''}|${f.model_id ?? ''}|${f.window_type ?? ''}|${f.variant ?? ''}`;
}

function forecastTitle(f: ForecastEntry): string {
  const parts = [f.service_name || f.model_id || 'quota'];
  if (f.window_type && f.window_type !== 'unknown') parts.push(f.window_type);
  return parts.join(' · ');
}

export function ForecastTab({
  providerId,
  accountId,
  entry,
}: {
  providerId: string;
  accountId: string;
  entry: FleetEntry;
}) {
  const forecast = useProviderForecast(providerId, accountId);
  const forecasts = useMemo(
    () => forecast.data?.forecasts ?? [],
    [forecast.data],
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const selected =
    forecasts.find((f) => forecastKey(f) === selectedKey) ?? forecasts[0] ?? null;

  const anomalies = useProviderAnomalies(providerId, accountId);
  const cost = useProviderCostForecast(providerId, accountId);
  const spikes = anomalies.data?.anomalies ?? [];

  const windowType = entry.critical_gauge.window_type ?? 'unknown';
  const history = useWindowHistory(providerId, accountId, windowType);
  // The archive stores one row per quota card variant; cards sharing a pool
  // produce identical windows — collapse them for display.
  const windows = useMemo(() => {
    const seen = new Set<string>();
    return (history.data?.windows ?? []).filter((w) => {
      const key = `${w.window_start}|${w.window_end}|${w.totals?.msgs}|${w.totals?.cost_usd}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [history.data]);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Trajectory</CardTitle>
          {forecasts.length > 1 ? (
            <Select
              value={selected ? forecastKey(selected) : ''}
              onValueChange={setSelectedKey}
            >
              <SelectTrigger className="h-8 max-w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {forecasts.map((f) => (
                  <SelectItem key={forecastKey(f)} value={forecastKey(f)}>
                    {forecastTitle(f)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
        </CardHeader>
        <CardContent>
          {forecast.isPending ? (
            <Skeleton className="h-56 w-full" />
          ) : !selected ? (
            <p className="py-8 text-center text-xs text-fg-subtle">No forecast available yet.</p>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-muted">
                <Badge variant={STATUS_VARIANT[selected.status] ?? 'neutral'}>
                  {selected.status.replace('_', ' ')}
                </Badge>
                <span>
                  now <strong className="font-mono tabular">{formatPct(selected.now_pct)}</strong>
                </span>
                <span>
                  projected at reset{' '}
                  <strong className="font-mono tabular">{formatPct(selected.projected_pct)}</strong>
                </span>
                {selected.projected_limit_hit_at ? (
                  <span className="text-critical">
                    limit hit in {timeUntil(selected.projected_limit_hit_at)} (
                    {formatLocalDateTime(selected.projected_limit_hit_at, {
                      weekday: 'short',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                    )
                  </span>
                ) : null}
                <span>
                  confidence{' '}
                  <strong className="font-mono tabular">
                    {selected.confidence != null ? `${Math.round(selected.confidence * 100)}%` : '—'}
                  </strong>{' '}
                  ({selected.samples_used ?? 0} samples)
                </span>
              </div>
              <TrajectoryChart forecast={selected} />
            </>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
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

        <Card>
          <CardHeader>
            <CardTitle>Usage anomalies</CardTitle>
            {anomalies.data ? (
              <span className="text-[11px] text-fg-subtle">
                today vs {anomalies.data.lookback_days}d mean
              </span>
            ) : null}
          </CardHeader>
          {anomalies.isPending ? (
            <CardContent>
              <Skeleton className="h-20 w-full" />
            </CardContent>
          ) : spikes.length === 0 ? (
            <CardContent>
              <p className="py-6 text-center text-xs text-fg-subtle">
                No usage anomalies detected.
              </p>
            </CardContent>
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH>Model</TH>
                  <TH className="text-right">Today</TH>
                  <TH className="text-right">Mean</TH>
                  <TH className="text-right">z-score</TH>
                </TR>
              </THead>
              <TBody>
                {spikes.map((a, i) => (
                  <TR key={`${a.model_id}-${i}`}>
                    <TD className="text-xs">{a.model_id}</TD>
                    <TD className="text-right font-mono tabular">{formatTokens(a.today_tokens)}</TD>
                    <TD className="text-right font-mono tabular">
                      {formatTokens(a.historical_mean_tokens)}
                    </TD>
                    <TD className="text-right font-mono tabular text-warning">
                      {a.z_score_tokens.toFixed(1)}σ
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>
            Past {windowType !== 'unknown' ? windowType : ''} windows
          </CardTitle>
        </CardHeader>
        {history.isPending && history.isFetching ? (
          <CardContent>
            <Skeleton className="h-24 w-full" />
          </CardContent>
        ) : windows.length === 0 ? (
          <CardContent>
            <p className="py-4 text-center text-xs text-fg-subtle">
              No closed windows archived yet.
            </p>
          </CardContent>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Window</TH>
                <TH className="text-right">Messages</TH>
                <TH className="text-right">Tokens</TH>
                <TH className="text-right">Cost</TH>
              </TR>
            </THead>
            <TBody>
              {windows.map((w, i) => {
                const tot = w.totals ?? {};
                const tokens =
                  (tot.tokens_input ?? 0) +
                  (tot.tokens_output ?? 0) +
                  (tot.tokens_cache_read ?? 0) +
                  (tot.tokens_cache_create ?? 0) +
                  (tot.tokens_reasoning ?? 0);
                return (
                  <TR key={`${w.window_start}-${i}`}>
                    <TD className="font-mono text-xs tabular">
                      {formatLocalDate(w.window_start)} – {formatLocalDate(w.window_end)}
                    </TD>
                    <TD className="text-right font-mono tabular">{tot.msgs ?? '—'}</TD>
                    <TD className="text-right font-mono tabular">{formatTokens(tokens)}</TD>
                    <TD className="text-right font-mono tabular">{formatCost(tot.cost_usd)}</TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        )}
      </Card>
    </div>
  );
}
