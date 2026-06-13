// History: fill curves / token / cost chart for one account, range deltas,
// the window archive (open + closed), and anomaly diagnostics.

import { useMemo, useState } from 'react';
import type { HistoryWindowRow } from '@/api/types';
import { PageHeader } from '@/components/layout/PageHeader';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatTile } from '@/components/ui/StatTile';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { useAnomalies, useFleet, useProviderConfigs } from '@/features/home/queries';
import { formatCost, formatPct, formatTokens } from '@/lib/format';
import { formatLocalDate } from '@/lib/tz';
import { HistoryChart } from './HistoryChart';
import { WindowDetailSheet } from './WindowDetailSheet';
import { useHistoryChart, useHistoryDeltas, useHistoryWindows, type Metric } from './queries';

const RANGES = [
  { days: 7, label: '7d' },
  { days: 14, label: '14d' },
  { days: 30, label: '30d' },
  { days: 90, label: '90d' },
];

export function HistoryPage() {
  const fleet = useFleet();
  const providerConfigs = useProviderConfigs();
  const anomalies = useAnomalies();
  const [days, setDays] = useState(7);
  const [metric, setMetric] = useState<Metric>('percent');
  const [accountKey, setAccountKey] = useState<string | null>(null);
  const [detailRow, setDetailRow] = useState<HistoryWindowRow | null>(null);

  const accounts = useMemo(() => {
    const names = new Map(
      (providerConfigs.data?.providers ?? []).map((p) => [p.provider_id, p.name]),
    );
    return (fleet.data?.fleet ?? []).map((e) => ({
      key: `${e.provider_id}:${e.account_id}`,
      providerId: e.provider_id,
      accountId: e.account_id,
      label: `${names.get(e.provider_id) ?? e.provider_id} — ${
        e.critical_gauge.account_label || e.account_id
      }`,
    }));
  }, [fleet.data, providerConfigs.data]);

  const selected = accounts.find((a) => a.key === accountKey) ?? accounts[0] ?? null;
  const chart = useHistoryChart(
    selected?.providerId ?? null,
    selected?.accountId ?? null,
    days,
    metric,
  );
  const deltas = useHistoryDeltas(days);
  const windows = useHistoryWindows(selected?.providerId ?? null, days);

  const hasChartData =
    (chart.data?.series?.some((s) => s.points.length > 0) ?? false) ||
    (chart.data?.bars?.length ?? 0) > 0;

  return (
    <>
      <PageHeader title="History" description="Usage over time" />
      <div className="flex flex-col gap-4 p-4 lg:p-8">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={selected?.key ?? ''} onValueChange={setAccountKey}>
            <SelectTrigger className="w-full max-w-72 sm:w-auto">
              <SelectValue placeholder="Select account" />
            </SelectTrigger>
            <SelectContent>
              {accounts.map((a) => (
                <SelectItem key={a.key} value={a.key}>
                  {a.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Tabs value={String(days)} onValueChange={(v) => setDays(Number(v))}>
            <TabsList className="border-0">
              {RANGES.map((r) => (
                <TabsTrigger key={r.days} value={String(r.days)} className="h-9 px-2.5">
                  {r.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          <Tabs value={metric} onValueChange={(v) => setMetric(v as Metric)} className="ml-auto">
            <TabsList className="border-0">
              <TabsTrigger value="percent" className="h-9 px-2.5">
                % used
              </TabsTrigger>
              <TabsTrigger value="tokens" className="h-9 px-2.5">
                Tokens
              </TabsTrigger>
              <TabsTrigger value="cost" className="h-9 px-2.5">
                Cost
              </TabsTrigger>
            </TabsList>
          </Tabs>
        </div>

        <Card>
          <CardContent className="pt-4">
            {chart.isPending || fleet.isPending ? (
              <Skeleton className="h-72 w-full" />
            ) : !hasChartData ? (
              <p className="py-16 text-center text-xs text-fg-subtle">
                No data points in this range.
              </p>
            ) : (
              <HistoryChart data={chart.data!} metric={metric} />
            )}
          </CardContent>
        </Card>

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            label={`Tokens (${days}d)`}
            value={formatTokens(deltas.data?.token_delta_total ?? 0)}
            loading={deltas.isPending}
          />
          <StatTile
            label={`Cost (${days}d)`}
            value={formatCost(deltas.data?.cost_delta_total)}
            loading={deltas.isPending}
          />
          <StatTile
            label="Critical series"
            value={String(deltas.data?.critical_series_count ?? 0)}
            loading={deltas.isPending}
          />
          <StatTile
            label="Anomalies today"
            value={String(anomalies.data?.anomalies.length ?? 0)}
            loading={anomalies.isPending}
          />
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Quota windows</CardTitle>
            <span className="text-[11px] text-fg-subtle">
              {selected ? selected.label : ''} · last {days}d
            </span>
          </CardHeader>
          {windows.isPending ? (
            <CardContent>
              <Skeleton className="h-32 w-full" />
            </CardContent>
          ) : (windows.data?.windows.length ?? 0) === 0 ? (
            <CardContent>
              <p className="py-6 text-center text-xs text-fg-subtle">No windows in this range.</p>
            </CardContent>
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH>Window</TH>
                  <TH>Service</TH>
                  <TH className="hidden sm:table-cell">Type</TH>
                  <TH className="text-right">Peak</TH>
                  <TH className="hidden text-right md:table-cell">Tokens</TH>
                  <TH className="hidden text-right md:table-cell">Cost</TH>
                  <TH className="hidden lg:table-cell">Top model</TH>
                </TR>
              </THead>
              <TBody>
                {windows.data!.windows.map((w, i) => (
                  <TR
                    key={`${w.provider_id}-${w.window_start}-${i}`}
                    onClick={() => setDetailRow(w)}
                    className="cursor-pointer hover:bg-surface-2"
                  >
                    <TD className="font-mono text-xs whitespace-nowrap tabular">
                      {w.window_start ? (
                        <>
                          {formatLocalDate(w.window_start)} – {formatLocalDate(w.window_end)}
                        </>
                      ) : (
                        '—'
                      )}
                      {w.is_open ? (
                        <Badge variant="accent" className="ml-2">
                          open
                        </Badge>
                      ) : null}
                    </TD>
                    <TD className="text-xs">{w.service_name ?? w.provider_id}</TD>
                    <TD className="hidden text-xs text-fg-muted sm:table-cell">{w.window_type}</TD>
                    <TD className="text-right font-mono tabular">
                      {w.pct_used != null ? formatPct(w.pct_used) : '—'}
                    </TD>
                    <TD className="hidden text-right font-mono tabular md:table-cell">
                      {w.tokens_total != null ? formatTokens(w.tokens_total) : '—'}
                    </TD>
                    <TD className="hidden text-right font-mono tabular md:table-cell">
                      {formatCost(w.cost_usd)}
                    </TD>
                    <TD className="hidden text-xs text-fg-muted lg:table-cell">
                      {w.top_model ?? '—'}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </Card>

        {(anomalies.data?.anomalies.length ?? 0) > 0 ? (
          <Card>
            <CardHeader>
              <CardTitle>Anomalies (today vs 30d mean)</CardTitle>
            </CardHeader>
            <Table>
              <THead>
                <TR>
                  <TH>Provider</TH>
                  <TH>Model</TH>
                  <TH className="text-right">Today</TH>
                  <TH className="text-right">30d mean</TH>
                  <TH className="text-right">z-score</TH>
                </TR>
              </THead>
              <TBody>
                {anomalies.data!.anomalies.map((a, i) => (
                  <TR key={`${a.provider_id}-${a.model_id}-${i}`}>
                    <TD className="text-xs">{a.provider_id}</TD>
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
          </Card>
        ) : null}
      </div>

      <WindowDetailSheet row={detailRow} onClose={() => setDetailRow(null)} />
    </>
  );
}
