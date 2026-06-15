// Top-sessions table with click-to-expand detail. The collapsed row stays
// scannable (session, models, duration, messages, tokens, cost); expanding a
// row reveals framed sections — the token breakdown (input/output/cache/
// reasoning) chips, the per-model split, and the per-agent (subagent) split,
// the latter two rendered as compact metric cards rather than dense tables.

import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import type { SessionEntry } from '@/api/types';
import { TokenBar } from '@/components/charts/TokenBar';
import { Badge } from '@/components/ui/Badge';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { cn } from '@/lib/cn';
import { formatCost, formatDuration, formatTokens } from '@/lib/format';
import { sessionCachePct, sessionCost, sessionTokens } from './sessionMetrics';

// Base column count (chevron + session + models + duration + messages + tokens +
// cost); the optional Sidecar column adds one when more than one host feeds the
// fleet. Used for the detail row's colSpan.
const BASE_COL_COUNT = 7;

/** Labelled token/number chip used across the detail panel. */
function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] tracking-wide text-fg-subtle uppercase">{label}</span>
      <span className="font-mono text-xs tabular text-fg">{value}</span>
    </div>
  );
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[11px] font-medium text-fg-muted">{title}</span>
      <div className="rounded-md border border-edge bg-surface-1 p-3">{children}</div>
    </div>
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
        </div>
      </DetailSection>

      {byModel.length > 0 ? (
        <DetailSection title="By model">
          <div className="grid gap-2 sm:grid-cols-2">
            {byModel.map((m) => (
              <MetricCard
                key={m.model_id}
                title={<Badge variant="neutral">{m.model_id}</Badge>}
                cost={m.cost_usd}
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
                cost={a.cost_usd}
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
  colSpan,
}: {
  s: SessionEntry;
  excludeCache: boolean;
  // Resolved sidecar label, or null to omit the column entirely (single-host).
  sidecarName: string | null;
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
        <TD className="text-right font-mono tabular">{formatCost(sessionCost(s))}</TD>
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
}: {
  sessions: SessionEntry[];
  excludeCache?: boolean;
}) {
  // Show the Sidecar column only when more than one host feeds the fleet.
  const sidecars = useSidecars().data?.sidecars ?? [];
  const showSidecar = sidecars.length > 1;
  const nameMap = showSidecar ? buildSidecarNameMap(sidecars) : null;
  const colSpan = BASE_COL_COUNT + (showSidecar ? 1 : 0);
  const labelFor = (s: SessionEntry): string | null =>
    nameMap ? (s.sidecar_id ? (nameMap.get(s.sidecar_id) ?? s.sidecar_id) : '') : null;

  return (
    <Table>
      <THead>
        <TR>
          <TH className="w-8" />
          <TH>Session</TH>
          <TH>Models</TH>
          {showSidecar ? <TH>Sidecar</TH> : null}
          <TH className="text-right">Duration</TH>
          <TH className="text-right">Messages</TH>
          <TH className="text-right">Tokens</TH>
          <TH className="text-right">Cost</TH>
        </TR>
      </THead>
      <TBody>
        {sessions.map((s) => (
          <SessionRow
            key={s.session_id}
            s={s}
            excludeCache={excludeCache}
            sidecarName={labelFor(s)}
            colSpan={colSpan}
          />
        ))}
      </TBody>
    </Table>
  );
}
