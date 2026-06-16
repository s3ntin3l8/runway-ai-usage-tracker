// Top-sessions table with click-to-expand detail. The collapsed row stays
// scannable (session, models, duration, messages, tokens, cost); expanding a
// row reveals framed sections — the token breakdown (input/output/cache/
// reasoning) chips, the per-model split, and the per-agent (subagent) split,
// the latter two rendered as compact metric cards rather than dense tables.

import { useState } from 'react';
import { ArrowDown, ArrowUp, ChevronRight } from 'lucide-react';
import type { SessionEntry } from '@/api/types';
import { TokenBar } from '@/components/charts/TokenBar';
import { Badge } from '@/components/ui/Badge';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { cn } from '@/lib/cn';
import { formatCost, formatDuration, formatTokens } from '@/lib/format';
import { DetailSection, Stat } from './detailPrimitives';
import { bucketCost, sessionCachePct, sessionCost, sessionTokens } from './sessionMetrics';

// Base column count (chevron + session + models + duration + messages + tokens +
// cost); the optional Sidecar and Project columns each add one. Used for the
// detail row's colSpan.
const BASE_COL_COUNT = 7;

// The clickable column headers map to server-side `sort_by` values; `recent`
// (default) has no header of its own, so it isn't part of this union.
export type SessionSortKey = 'duration' | 'messages' | 'tokens' | 'cost';
export type SortDir = 'asc' | 'desc';

/** A right-aligned, clickable column header with an active asc/desc indicator. */
function SortableTH({
  label,
  sortKey,
  sort,
  onSort,
}: {
  label: string;
  sortKey: SessionSortKey;
  sort?: { by: SessionSortKey | 'recent'; dir: SortDir };
  onSort: (key: SessionSortKey) => void;
}) {
  const active = sort?.by === sortKey;
  return (
    <TH
      className="text-right"
      aria-sort={active ? (sort!.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          'ml-auto flex items-center gap-1 hover:text-fg',
          active && 'text-fg',
        )}
      >
        {label}
        {active ? (
          sort!.dir === 'asc' ? (
            <ArrowUp className="size-3" aria-hidden />
          ) : (
            <ArrowDown className="size-3" aria-hidden />
          )
        ) : null}
      </button>
    </TH>
  );
}

/** Compact card for one model/agent row — a titled header plus a metric grid. */
function MetricCard({
  title,
  cost,
  metrics,
}: {
  title: React.ReactNode;
  cost?: number | null;
  metrics: { label: string; value: string }[];
}) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-edge bg-surface-2/40 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate">{title}</span>
        <span className="font-mono text-xs tabular text-fg">{formatCost(cost)}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 sm:grid-cols-3">
        {metrics.map((m) => (
          <Stat key={m.label} label={m.label} value={m.value} />
        ))}
      </div>
    </div>
  );
}

function SessionDetail({ s, excludeCache }: { s: SessionEntry; excludeCache: boolean }) {
  const cacheRead = s.tokens_cache_read ?? 0;
  const cacheCreate = s.tokens_cache_create ?? 0;
  const reasoning = s.tokens_reasoning ?? 0;
  const toolCalls = s.tool_calls ?? 0;
  const cachePct = sessionCachePct(s);
  const byModel = s.by_model ?? [];
  const subagents = s.subagents ?? [];

  return (
    <div className="flex flex-col gap-5 bg-surface-2/40 px-4 py-4">
      <DetailSection title="Token breakdown">
        <div className="flex flex-col gap-3">
          {/* Bar shows proportion only — the Stat tiles below carry the labels
              and values (with extras), so a legend here would just duplicate them.
              Per-segment values remain on hover. */}
          <TokenBar tokens={s} excludeCache={excludeCache} />
          <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
            <Stat label="Input" value={formatTokens(s.tokens_input ?? 0)} />
            <Stat label="Output" value={formatTokens(s.tokens_output ?? 0)} />
            <Stat label="Cache read" value={formatTokens(cacheRead)} />
            <Stat label="Cache write" value={formatTokens(cacheCreate)} />
            {reasoning > 0 ? <Stat label="Reasoning" value={formatTokens(reasoning)} /> : null}
            <Stat label="Tool calls" value={toolCalls.toLocaleString()} />
            {cachePct != null ? <Stat label="Cache" value={`${cachePct}%`} /> : null}
          </div>
          {/* Cost per category, paired with the token grid above. Reasoning is
              billed at the output rate, so it folds into Output (no own cell).
              Cache costs drop out when the exclude-cache toggle is on, matching
              the token bar and the headline Cost. */}
          <div className="border-t border-edge pt-3">
            <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
              <Stat label="Input $" value={formatCost(s.cost_input ?? 0)} />
              <Stat label="Output $" value={formatCost(s.cost_output ?? 0)} />
              {excludeCache ? null : (
                <>
                  <Stat label="Cache read $" value={formatCost(s.cost_cache_read ?? 0)} />
                  <Stat label="Cache write $" value={formatCost(s.cost_cache_create ?? 0)} />
                </>
              )}
              <Stat label="Total $" value={formatCost(sessionCost(s, excludeCache))} />
            </div>
          </div>
        </div>
      </DetailSection>

      {byModel.length > 0 ? (
        <DetailSection title="By model">
          <div className="grid gap-2 sm:grid-cols-2">
            {byModel.map((m) => (
              <MetricCard
                key={m.model_id}
                title={<Badge variant="neutral">{m.model_id}</Badge>}
                cost={bucketCost(m, excludeCache)}
                metrics={[
                  { label: 'Msgs', value: (m.msgs ?? 0).toLocaleString() },
                  { label: 'Tools', value: (m.tool_calls ?? 0).toLocaleString() },
                  { label: 'Tokens', value: formatTokens(m.tokens_total ?? 0) },
                  { label: 'In', value: formatTokens(m.tokens_input ?? 0) },
                  { label: 'Out', value: formatTokens(m.tokens_output ?? 0) },
                  {
                    label: 'Cache',
                    value: formatTokens((m.tokens_cache_read ?? 0) + (m.tokens_cache_create ?? 0)),
                  },
                ]}
              />
            ))}
          </div>
        </DetailSection>
      ) : null}

      <DetailSection title="Agents">
        {subagents.length > 0 ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {subagents.map((a) => (
              <MetricCard
                key={a.subagent_type}
                title={<span className="text-xs font-medium text-fg">{a.subagent_type}</span>}
                cost={bucketCost(a, excludeCache)}
                metrics={[
                  { label: 'Turns', value: (a.turns ?? 0).toLocaleString() },
                  { label: 'Tools', value: (a.tool_calls ?? 0).toLocaleString() },
                  { label: 'Tokens', value: formatTokens(a.tokens_total ?? 0) },
                ]}
              />
            ))}
          </div>
        ) : (
          <span className="text-xs text-fg-subtle">No subagents — main session only.</span>
        )}
      </DetailSection>
    </div>
  );
}

function SessionRow({
  s,
  excludeCache,
  sidecarName,
  showProject,
  colSpan,
}: {
  s: SessionEntry;
  excludeCache: boolean;
  // Resolved sidecar label, or null to omit the column entirely (single-host).
  sidecarName: string | null;
  // Whether to render the Project column (only when some session has a project).
  showProject: boolean;
  colSpan: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <TR
        className="cursor-pointer hover:bg-surface-2/50"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <TD className="w-8 pr-0 text-fg-subtle">
          <ChevronRight
            className={cn('size-4 transition-transform duration-150', open && 'rotate-90')}
          />
        </TD>
        <TD className="max-w-32 truncate font-mono text-xs" title={s.session_id}>
          {s.session_id.slice(0, 8)}
        </TD>
        <TD>
          <span className="flex flex-wrap gap-1">
            {(s.models ?? []).map((m) => (
              <Badge key={m} variant="neutral">
                {m}
              </Badge>
            ))}
          </span>
        </TD>
        {showProject ? (
          <TD>
            {s.project ? (
              <Badge variant="neutral" title={s.cwd ?? undefined}>
                {s.project}
              </Badge>
            ) : (
              <span className="text-fg-subtle">—</span>
            )}
          </TD>
        ) : null}
        {sidecarName !== null ? (
          <TD>
            {sidecarName ? (
              <Badge variant="neutral">{sidecarName}</Badge>
            ) : (
              <span className="text-fg-subtle">—</span>
            )}
          </TD>
        ) : null}
        <TD className="text-right font-mono tabular">
          {s.duration_seconds != null ? formatDuration(s.duration_seconds * 1000) : '—'}
        </TD>
        <TD className="text-right font-mono tabular">{s.msgs ?? 0}</TD>
        <TD className="text-right font-mono tabular">
          {formatTokens(sessionTokens(s, excludeCache))}
        </TD>
        <TD className="text-right font-mono tabular">
          {formatCost(sessionCost(s, excludeCache))}
        </TD>
      </TR>
      {open ? (
        <TR className="hover:bg-transparent">
          <TD colSpan={colSpan} className="p-0">
            <SessionDetail s={s} excludeCache={excludeCache} />
          </TD>
        </TR>
      ) : null}
    </>
  );
}

export function SessionsTable({
  sessions,
  excludeCache = false,
  sort,
  onSort,
}: {
  sessions: SessionEntry[];
  excludeCache?: boolean;
  // Current sort state + click handler — provided only by the paginated
  // Sessions browser. When omitted (e.g. the Activity-tab top-10), the
  // Duration/Messages/Tokens/Cost headers render as plain, non-interactive text.
  sort?: { by: SessionSortKey | 'recent'; dir: SortDir };
  onSort?: (key: SessionSortKey) => void;
}) {
  // Show the Sidecar column only when more than one host feeds the fleet.
  const sidecars = useSidecars().data?.sidecars ?? [];
  const showSidecar = sidecars.length > 1;
  const nameMap = showSidecar ? buildSidecarNameMap(sidecars) : null;
  // Show the Project column only when at least one session is attributed (so
  // non-logging providers / pre-backfill data don't get an empty column).
  const showProject = sessions.some((s) => s.project);
  const colSpan = BASE_COL_COUNT + (showSidecar ? 1 : 0) + (showProject ? 1 : 0);
  const labelFor = (s: SessionEntry): string | null =>
    nameMap ? (s.sidecar_id ? (nameMap.get(s.sidecar_id) ?? s.sidecar_id) : '') : null;

  return (
    <Table>
      <THead>
        <TR>
          <TH className="w-8" />
          <TH>Session</TH>
          <TH>Models</TH>
          {showProject ? <TH>Project</TH> : null}
          {showSidecar ? <TH>Sidecar</TH> : null}
          {onSort ? (
            <>
              <SortableTH label="Duration" sortKey="duration" sort={sort} onSort={onSort} />
              <SortableTH label="Messages" sortKey="messages" sort={sort} onSort={onSort} />
              <SortableTH label="Tokens" sortKey="tokens" sort={sort} onSort={onSort} />
              <SortableTH label="Cost" sortKey="cost" sort={sort} onSort={onSort} />
            </>
          ) : (
            <>
              <TH className="text-right">Duration</TH>
              <TH className="text-right">Messages</TH>
              <TH className="text-right">Tokens</TH>
              <TH className="text-right">Cost</TH>
            </>
          )}
        </TR>
      </THead>
      <TBody>
        {sessions.map((s) => (
          <SessionRow
            key={s.session_id}
            s={s}
            excludeCache={excludeCache}
            sidecarName={labelFor(s)}
            showProject={showProject}
            colSpan={colSpan}
          />
        ))}
      </TBody>
    </Table>
  );
}
