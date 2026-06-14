// Events: the raw per-message event stream for this account, scoped to the
// selected month (defaults to the current month) and paged. Promoted out of the
// Activity tab so the "what did I actually do?" data is a first-class view.

import { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { formatCost, formatTokens } from '@/lib/format';
import { formatLocalDate, formatLocalDateTime } from '@/lib/tz';
import type { SelectedPeriod } from './period';
import { useProviderEventsPage } from './queries';

const PAGE_SIZE = 25;

export function EventsTab({
  providerId,
  accountId,
  period,
  active,
}: {
  providerId: string;
  accountId: string;
  period: SelectedPeriod;
  active: boolean;
}) {
  const [page, setPage] = useState(0);
  // Reset to the first page whenever the selected month changes — the old
  // offset is meaningless against a different month's total.
  useEffect(() => setPage(0), [period.key]);
  const q = useProviderEventsPage(providerId, accountId, {
    page,
    pageSize: PAGE_SIZE,
    since: period.range.since,
    until: period.range.until,
    enabled: active,
  });

  const monthLabel = formatLocalDate(period.range.since, { month: 'long', year: 'numeric' });
  const total = q.data?.total ?? 0;
  const events = q.data?.events ?? [];
  const start = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const end = Math.min((page + 1) * PAGE_SIZE, total);
  const hasNext = (page + 1) * PAGE_SIZE < total;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Events · {monthLabel}</CardTitle>
        {q.data ? (
          <span className="text-[11px] text-fg-subtle tabular">
            {total > 0 ? `${start}–${end} of ${total}` : '0'}
          </span>
        ) : null}
      </CardHeader>

      {q.isPending ? (
        <CardContent>
          <Skeleton className="h-96 w-full" />
        </CardContent>
      ) : total === 0 ? (
        <CardContent>
          <p className="py-12 text-center text-xs text-fg-subtle">
            No events in {monthLabel} — per-message events arrive via sidecar ingest.
          </p>
        </CardContent>
      ) : (
        <>
          <Table>
            <THead>
              <TR>
                <TH>Time</TH>
                <TH>Model</TH>
                <TH className="hidden md:table-cell">Sidecar</TH>
                <TH className="text-right">In</TH>
                <TH className="text-right">Out</TH>
                <TH className="hidden text-right sm:table-cell">Cache</TH>
                <TH className="text-right">Cost</TH>
              </TR>
            </THead>
            <TBody>
              {events.map((e) => (
                <TR key={e.event_id ?? e.id}>
                  <TD className="font-mono text-xs whitespace-nowrap tabular">
                    {formatLocalDateTime(e.ts, {
                      month: 'short',
                      day: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </TD>
                  <TD>
                    {e.kind === 'error' ? (
                      <Badge variant="critical">error</Badge>
                    ) : (
                      <span className="text-xs">{e.model_id ?? '—'}</span>
                    )}
                  </TD>
                  <TD className="hidden text-xs text-fg-muted md:table-cell">
                    {e.sidecar_id ?? '—'}
                  </TD>
                  <TD className="text-right font-mono text-xs tabular">
                    {formatTokens(e.tokens_input ?? 0)}
                  </TD>
                  <TD className="text-right font-mono text-xs tabular">
                    {formatTokens(e.tokens_output ?? 0)}
                  </TD>
                  <TD className="hidden text-right font-mono text-xs tabular sm:table-cell">
                    {formatTokens((e.tokens_cache_read ?? 0) + (e.tokens_cache_create ?? 0))}
                  </TD>
                  <TD className="text-right font-mono text-xs tabular">{formatCost(e.cost_usd)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>

          <div className="flex items-center justify-between gap-2 border-t border-edge px-4 py-3">
            <span className="text-[11px] text-fg-subtle tabular">
              Showing {start}–{end} of {total}
            </span>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                aria-label="Previous page"
              >
                <ChevronLeft className="size-3.5" aria-hidden /> Prev
              </Button>
              <Button
                size="sm"
                variant="secondary"
                disabled={!hasNext}
                onClick={() => setPage((p) => p + 1)}
                aria-label="Next page"
              >
                Next <ChevronRight className="size-3.5" aria-hidden />
              </Button>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}
