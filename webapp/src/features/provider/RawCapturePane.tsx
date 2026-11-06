import { useState } from 'react';
import { Bug } from 'lucide-react';
import type { DebugRawResponse } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { useDebugRaw } from './queries';
import { StrategySection } from './StrategySection';

export function RawCapturePane({
  providerId,
  accountId,
  active,
  captureSupported,
}: {
  providerId: string;
  accountId: string;
  active: boolean;
  captureSupported: boolean;
}) {
  const [requested, setRequested] = useState(false);
  const debug = useDebugRaw(providerId, accountId, captureSupported && active && requested);

  if (!captureSupported) {
    return (
      <EmptyState
        icon={Bug}
        title="Raw capture unavailable"
        description={`${providerId} has no server quota collector, so there is no server-side HTTP exchange to capture.`}
      />
    );
  }

  if (!requested) {
    return (
      <EmptyState
        icon={Bug}
        title="Capture raw collector output"
        description="Runs this provider's collector once and records the upstream HTTP exchanges (secrets redacted). Admin only; rate-limited."
        action={
          <Button variant="primary" size="sm" onClick={() => setRequested(true)}>
            Run capture
          </Button>
        }
      />
    );
  }

  if (debug.isPending) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-6 w-1/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (debug.isError) {
    return (
      <EmptyState
        icon={Bug}
        title="Capture failed"
        description={debug.error.message}
        action={
          <Button size="sm" onClick={() => debug.refetch()}>
            Retry
          </Button>
        }
      />
    );
  }

  const data = debug.data as DebugRawResponse;
  const strategyIds = Object.keys(data.strategies ?? {});

  return (
    <Card>
      <CardHeader>
        <CardTitle>Raw collector exchange</CardTitle>
        <Button size="sm" variant="ghost" onClick={() => debug.refetch()} loading={debug.isFetching}>
          Re-run
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {strategyIds.length === 0 ? (
          <div className="text-[12px] text-fg-muted">
            No per-strategy breakdown available (legacy collector or no strategies declared).
          </div>
        ) : (
          strategyIds.map((sId) => {
            const cap = data.strategies[sId];
            return (
              <StrategySection
                key={sId}
                strategyId={sId}
                capture={cap}
                isActive={sId === data.active_strategy}
              />
            );
          })
        )}
      </CardContent>
    </Card>
  );
}
