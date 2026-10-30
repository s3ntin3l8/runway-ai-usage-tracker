// Sessions: the full, paginated session list for this account scoped to the
// selected month (defaults to current) — the "browse everything" companion to
// the top-10 sessions on the Activity tab. Same row format, 25 per page, with
// an optional project filter (set from the provider Filters popover).

import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { useProjectFilter } from '@/hooks/useProjectFilter';
import { useBranchFilter, useHideSubagents, useModelFilter } from '@/hooks/useSessionFilters';
import { useUsageSource } from '@/hooks/useUsageSource';
import { SessionsTable, type SessionSortKey, type SortDir } from './SessionsTable';
import type { TabScope } from './period';
import { useSessionsPaginated } from './queries';

const PAGE_SIZE = 25;

export function SessionsBrowser({
  providerId,
  accountId,
  scope,
  active,
}: {
  providerId: string;
  accountId: string;
  scope: TabScope;
  active: boolean;
}) {
  const { excludeCache } = useExcludeCache();
  const [sidecarId] = useUsageSource();
  const [project, setProject] = useProjectFilter();
  const [model] = useModelFilter();
  const [branch] = useBranchFilter();
  const [hideSubagents] = useHideSubagents();
  const [page, setPage] = useState(0);
  // Default sort mirrors the server default ('recent' desc) — no header is
  // marked active until the user clicks one.
  const [sortBy, setSortBy] = useState<SessionSortKey | 'recent'>('recent');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  // Reset to the first page whenever the scope, a filter, or the sort changes — the
  // old offset is meaningless against a reordered or differently-sized result.
  // A project only exists within one source, so switching source clears it —
  // but not on mount, or a reload would drop the project from the URL.
  const previousSidecar = useRef(sidecarId);
  useEffect(() => {
    if (previousSidecar.current === sidecarId) return;
    previousSidecar.current = sidecarId;
    setPage(0);
    setProject(undefined);
  }, [sidecarId, setProject]);
  useEffect(() => setPage(0), [scope.key, project, model, branch, hideSubagents, sortBy, sortDir]);

  // Toggle direction when re-clicking the active column; otherwise switch
  // column and start descending (largest-first, the common intent).
  const handleSort = (key: SessionSortKey) => {
    if (key === sortBy) {
      setSortDir((d) => (d === 'desc' ? 'asc' : 'desc'));
    } else {
      setSortBy(key);
      setSortDir('desc');
    }
  };

  const q = useSessionsPaginated(providerId, accountId, {
    page,
    pageSize: PAGE_SIZE,
    since: scope.range.since,
    until: scope.range.until,
    project: project ?? null,
    model: model ?? null,
    branch: branch ?? null,
    hideSubagents,
    sortBy,
    sortDir,
    enabled: active,
  });

  const scopeLabel = scope.label;
  const total = q.data?.total ?? 0;
  const sessions = q.data?.sessions ?? [];
  const start = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const end = Math.min((page + 1) * PAGE_SIZE, total);
  const hasNext = (page + 1) * PAGE_SIZE < total;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Sessions · {scopeLabel}</CardTitle>
      </CardHeader>

      {q.isPending ? (
        <CardContent>
          <Skeleton className="h-96 w-full" />
        </CardContent>
      ) : total === 0 ? (
        <CardContent>
          <p className="py-12 text-center text-xs text-fg-subtle">
            No sessions in {scopeLabel}
            {project ? ` for ${project}` : ''} — sessions arrive via sidecar ingest.
          </p>
        </CardContent>
      ) : (
        <>
          <SessionsTable
            sessions={sessions}
            excludeCache={excludeCache}
            sort={{ by: sortBy, dir: sortDir }}
            onSort={handleSort}
          />
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
