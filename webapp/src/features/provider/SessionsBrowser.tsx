// Sessions: the full, paginated session list for this account scoped to the
// selected month (defaults to current) — the "browse everything" companion to
// the top-10 sessions on the Activity tab. Same row format, 25 per page, with
// an optional project filter.

import { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { Skeleton } from '@/components/ui/Skeleton';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { ExcludeCacheToggle } from '@/components/ui/ExcludeCacheToggle';
import { formatLocalDate } from '@/lib/tz';
import { SessionsTable, type SessionSortKey, type SortDir } from './SessionsTable';
import type { SelectedPeriod } from './period';
import { useProjects, useSessionsPaginated } from './queries';

const PAGE_SIZE = 25;
const ALL = '__all__';

export function SessionsBrowser({
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
  const { excludeCache } = useExcludeCache();
  const [page, setPage] = useState(0);
  const [project, setProject] = useState<string>(ALL);
  // Default sort mirrors the server default ('recent' desc) — no header is
  // marked active until the user clicks one.
  const [sortBy, setSortBy] = useState<SessionSortKey | 'recent'>('recent');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  // Reset to the first page whenever the month, project, or sort changes — the
  // old offset is meaningless against a reordered or differently-sized result.
  useEffect(() => setPage(0), [period.key, project, sortBy, sortDir]);

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

  const projects = useProjects(providerId, period.range);
  const q = useSessionsPaginated(providerId, accountId, {
    page,
    pageSize: PAGE_SIZE,
    since: period.range.since,
    until: period.range.until,
    project: project === ALL ? null : project,
    sortBy,
    sortDir,
    enabled: active,
  });

  const monthLabel = formatLocalDate(period.range.since, { month: 'long', year: 'numeric' });
  const total = q.data?.total ?? 0;
  const sessions = q.data?.sessions ?? [];
  const start = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const end = Math.min((page + 1) * PAGE_SIZE, total);
  const hasNext = (page + 1) * PAGE_SIZE < total;
  const projectOptions = projects.data?.projects ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Sessions · {monthLabel}</CardTitle>
        <div className="flex items-center gap-3">
          {projectOptions.length > 0 ? (
            <Select value={project} onValueChange={setProject}>
              <SelectTrigger className="h-8 w-44">
                <SelectValue placeholder="All projects" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All projects</SelectItem>
                {projectOptions.map((p) => (
                  <SelectItem key={p} value={p}>
                    {p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
          <ExcludeCacheToggle />
        </div>
      </CardHeader>

      {q.isPending ? (
        <CardContent>
          <Skeleton className="h-96 w-full" />
        </CardContent>
      ) : total === 0 ? (
        <CardContent>
          <p className="py-12 text-center text-xs text-fg-subtle">
            No sessions in {monthLabel}
            {project === ALL ? '' : ` for ${project}`} — sessions arrive via sidecar ingest.
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
