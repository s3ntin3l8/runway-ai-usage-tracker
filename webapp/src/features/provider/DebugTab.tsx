// Debug: on-demand capture of raw upstream collector responses
// (admin-gated, runs live HTTP calls — never auto-fetches).

import { useState } from 'react';
import { Bug } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { useDebugRaw } from './queries';

export function DebugTab({ providerId, active }: { providerId: string; active: boolean }) {
  const [requested, setRequested] = useState(false);
  const debug = useDebugRaw(providerId, active && requested);

  if (!requested) {
    return (
      <EmptyState
        icon={Bug}
        title="Capture raw collector output"
        description="Runs this provider's collector once and records the upstream HTTP exchanges (auth headers masked). Admin only; rate-limited."
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

  return (
    <Card>
      <CardHeader>
        <CardTitle>Raw collector exchange</CardTitle>
        <Button size="sm" variant="ghost" onClick={() => debug.refetch()} loading={debug.isFetching}>
          Re-run
        </Button>
      </CardHeader>
      <CardContent>
        <pre className="max-h-[32rem] overflow-auto rounded-sm bg-surface-2 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
          {JSON.stringify(debug.data, null, 2)}
        </pre>
      </CardContent>
    </Card>
  );
}
