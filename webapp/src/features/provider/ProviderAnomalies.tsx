import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { formatTokens } from '@/lib/format';
import { useProviderAnomalies } from './queries';

export function ProviderAnomalies({
  providerId,
  accountId,
}: {
  providerId: string;
  accountId: string;
}) {
  const anomalies = useProviderAnomalies(providerId, accountId);
  const spikes = anomalies.data?.anomalies ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Usage anomalies</CardTitle>
        {anomalies.data ? (
          <span className="text-[11px] text-fg-subtle">
            today vs prior {anomalies.data.lookback_days} days
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
            {spikes.map((anomaly, index) => (
              <TR key={`${anomaly.model_id}-${index}`}>
                <TD className="text-xs">{anomaly.model_id}</TD>
                <TD className="text-right font-mono tabular">
                  {formatTokens(anomaly.today_tokens)}
                </TD>
                <TD className="text-right font-mono tabular">
                  {formatTokens(anomaly.historical_mean_tokens)}
                </TD>
                <TD className="text-right font-mono tabular text-warning">
                  {anomaly.z_score_tokens.toFixed(1)}σ
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </Card>
  );
}
