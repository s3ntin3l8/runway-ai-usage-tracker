// Top-sessions table with click-to-expand detail. The collapsed row stays
// scannable (session, models, duration, messages, tokens, cost); expanding a
// row reveals the token breakdown (input/output/cache/reasoning), the
// per-model split, the per-agent (subagent) split with their own tokens/tool
// calls, and total tool calls.

import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import type { SessionEntry } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { cn } from '@/lib/cn';
import { formatCost, formatDuration, formatTokens } from '@/lib/format';

const COL_COUNT = 7;

function sessionTokens(s: SessionEntry): number {
  return s.tokens_total ?? (s.by_model ?? []).reduce((sum, m) => sum + (m.tokens_total ?? 0), 0);
}

function sessionCost(s: SessionEntry): number {
  return s.cost_usd ?? (s.by_model ?? []).reduce((sum, m) => sum + (m.cost_usd ?? 0), 0);
}

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
      {children}
    </div>
  );
}

function SessionDetail({ s }: { s: SessionEntry }) {
  const cacheRead = s.tokens_cache_read ?? 0;
  const cacheCreate = s.tokens_cache_create ?? 0;
  const reasoning = s.tokens_reasoning ?? 0;
  const toolCalls = s.tool_calls ?? 0;
  const byModel = s.by_model ?? [];
  const subagents = s.subagents ?? [];

  return (
    <div className="flex flex-col gap-5 bg-surface-2/40 px-4 py-4">
      <DetailSection title="Token breakdown">
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <Stat label="Input" value={formatTokens(s.tokens_input ?? 0)} />
          <Stat label="Output" value={formatTokens(s.tokens_output ?? 0)} />
          <Stat label="Cache read" value={formatTokens(cacheRead)} />
          <Stat label="Cache write" value={formatTokens(cacheCreate)} />
          {reasoning > 0 ? <Stat label="Reasoning" value={formatTokens(reasoning)} /> : null}
          <Stat label="Tool calls" value={toolCalls.toLocaleString()} />
        </div>
      </DetailSection>

      {byModel.length > 0 ? (
        <DetailSection title="By model">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-fg-subtle">
                <tr className="text-left">
                  <th className="py-1 pr-4 font-medium">Model</th>
                  <th className="py-1 pr-4 text-right font-medium">Msgs</th>
                  <th className="py-1 pr-4 text-right font-medium">In</th>
                  <th className="py-1 pr-4 text-right font-medium">Out</th>
                  <th className="py-1 pr-4 text-right font-medium">Cache</th>
                  <th className="py-1 pr-4 text-right font-medium">Tools</th>
                  <th className="py-1 pr-4 text-right font-medium">Tokens</th>
                  <th className="py-1 text-right font-medium">Cost</th>
                </tr>
              </thead>
              <tbody className="font-mono tabular text-fg">
                {byModel.map((m) => (
                  <tr key={m.model_id}>
                    <td className="py-1 pr-4 font-sans">
                      <Badge variant="neutral">{m.model_id}</Badge>
                    </td>
                    <td className="py-1 pr-4 text-right">{m.msgs ?? 0}</td>
                    <td className="py-1 pr-4 text-right">{formatTokens(m.tokens_input ?? 0)}</td>
                    <td className="py-1 pr-4 text-right">{formatTokens(m.tokens_output ?? 0)}</td>
                    <td className="py-1 pr-4 text-right">
                      {formatTokens((m.tokens_cache_read ?? 0) + (m.tokens_cache_create ?? 0))}
                    </td>
                    <td className="py-1 pr-4 text-right">{(m.tool_calls ?? 0).toLocaleString()}</td>
                    <td className="py-1 pr-4 text-right">{formatTokens(m.tokens_total ?? 0)}</td>
                    <td className="py-1 text-right">{formatCost(m.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </DetailSection>
      ) : null}

      <DetailSection title="Agents">
        {subagents.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-fg-subtle">
                <tr className="text-left">
                  <th className="py-1 pr-4 font-medium">Agent</th>
                  <th className="py-1 pr-4 text-right font-medium">Turns</th>
                  <th className="py-1 pr-4 text-right font-medium">Tools</th>
                  <th className="py-1 pr-4 text-right font-medium">Tokens</th>
                  <th className="py-1 text-right font-medium">Cost</th>
                </tr>
              </thead>
              <tbody className="font-mono tabular text-fg">
                {subagents.map((a) => (
                  <tr key={a.subagent_type}>
                    <td className="py-1 pr-4 font-sans">{a.subagent_type}</td>
                    <td className="py-1 pr-4 text-right">{a.turns ?? 0}</td>
                    <td className="py-1 pr-4 text-right">{(a.tool_calls ?? 0).toLocaleString()}</td>
                    <td className="py-1 pr-4 text-right">{formatTokens(a.tokens_total ?? 0)}</td>
                    <td className="py-1 text-right">{formatCost(a.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <span className="text-xs text-fg-subtle">No subagents — main session only.</span>
        )}
      </DetailSection>
    </div>
  );
}

function SessionRow({ s }: { s: SessionEntry }) {
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
        <TD className="text-right font-mono tabular">
          {s.duration_seconds != null ? formatDuration(s.duration_seconds * 1000) : '—'}
        </TD>
        <TD className="text-right font-mono tabular">{s.msgs ?? 0}</TD>
        <TD className="text-right font-mono tabular">{formatTokens(sessionTokens(s))}</TD>
        <TD className="text-right font-mono tabular">{formatCost(sessionCost(s))}</TD>
      </TR>
      {open ? (
        <TR className="hover:bg-transparent">
          <TD colSpan={COL_COUNT} className="p-0">
            <SessionDetail s={s} />
          </TD>
        </TR>
      ) : null}
    </>
  );
}

export function SessionsTable({ sessions }: { sessions: SessionEntry[] }) {
  return (
    <Table>
      <THead>
        <TR>
          <TH className="w-8" />
          <TH>Session</TH>
          <TH>Models</TH>
          <TH className="text-right">Duration</TH>
          <TH className="text-right">Messages</TH>
          <TH className="text-right">Tokens</TH>
          <TH className="text-right">Cost</TH>
        </TR>
      </THead>
      <TBody>
        {sessions.map((s) => (
          <SessionRow key={s.session_id} s={s} />
        ))}
      </TBody>
    </Table>
  );
}
