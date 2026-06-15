// Overview activity pulse: compact stat cards for the 3 most recent sessions
// (by end time). Distinct from Activity's "Top sessions" table, which ranks by
// token volume — this answers "what did I just do?" at a glance.

import type { SessionEntry } from '@/api/types';
import { TokenBar } from '@/components/charts/TokenBar';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { formatCost, formatDuration, formatTokens, timeAgo } from '@/lib/format';
import { useProviderRecentSessions } from './queries';
import { sessionCachePct, sessionCost, sessionTokens } from './sessionMetrics';

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] tracking-wide text-fg-subtle uppercase">{label}</span>
      <span className="font-mono text-xs tabular text-fg">{value}</span>
    </div>
  );
}

function SessionCard({
  s,
  excludeCache,
  sidecarName,
}: {
  s: SessionEntry;
  excludeCache: boolean;
  // Resolved sidecar label, or null to hide (single-host setups).
  sidecarName: string | null;
}) {
  const cachePct = sessionCachePct(s);
  return (
    <Card>
      <CardContent className="flex flex-col gap-3 pt-4">
        <div className="flex items-baseline justify-between gap-2">
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="font-mono text-xs" title={s.session_id}>
              {s.session_id.slice(0, 8)}
            </span>
            {sidecarName ? (
              <Badge variant="outline" className="shrink-0">
                {sidecarName}
              </Badge>
            ) : null}
          </span>
          <span className="text-[11px] text-fg-subtle">ended {timeAgo(s.ts_end)}</span>
        </div>

        {(s.models ?? []).length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {(s.models ?? []).map((m) => (
              <Badge key={m} variant="neutral">
                {m}
              </Badge>
            ))}
          </span>
        ) : null}

        <div className="grid grid-cols-2 gap-x-4 gap-y-2.5">
          <Stat label="Duration" value={formatDuration((s.duration_seconds ?? 0) * 1000)} />
          <Stat label="Messages" value={(s.msgs ?? 0).toLocaleString()} />
          <Stat label="Tokens" value={formatTokens(sessionTokens(s, excludeCache))} />
          <Stat label="Cost" value={formatCost(sessionCost(s, excludeCache))} />
        </div>

        <TokenBar tokens={s} excludeCache={excludeCache} showLegend />
        {cachePct != null ? (
          <span className="text-[10px] text-fg-subtle">{cachePct}% cache</span>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function RecentSessions({
  providerId,
  accountId,
  excludeCache = false,
}: {
  providerId: string;
  accountId: string;
  excludeCache?: boolean;
}) {
  const q = useProviderRecentSessions(providerId, accountId);
  const sessions = q.data?.sessions ?? [];

  // Resolve sidecar labels only when more than one host feeds the fleet —
  // otherwise the origin is unambiguous and the badge is just noise.
  const sidecars = useSidecars().data?.sidecars ?? [];
  const sidecarNames = sidecars.length > 1 ? buildSidecarNameMap(sidecars) : null;
  const labelFor = (s: SessionEntry): string | null =>
    sidecarNames && s.sidecar_id ? (sidecarNames.get(s.sidecar_id) ?? s.sidecar_id) : null;

  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-[13px] font-semibold tracking-tight">Recent sessions</h2>
      {q.isPending ? (
        <div className="grid gap-4 sm:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-36 w-full" />
          ))}
        </div>
      ) : sessions.length === 0 ? (
        <p className="py-6 text-center text-xs text-fg-subtle">
          No sessions yet — session data needs a sidecar feeding events.
        </p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-3">
          {sessions.slice(0, 3).map((s) => (
            <SessionCard
              key={s.session_id}
              s={s}
              excludeCache={excludeCache}
              sidecarName={labelFor(s)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
