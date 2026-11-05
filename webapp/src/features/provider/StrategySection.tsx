import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { StrategyCapture } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { ResponseBlock } from './ResponseBlock';
import { Section } from './Section';

export function StrategySection({
  strategyId,
  capture,
  isActive,
}: {
  strategyId: string;
  capture: StrategyCapture;
  isActive: boolean;
}) {
  const [open, setOpen] = useState(false);

  const statusBadge: { variant: 'ok' | 'critical' | 'neutral'; label: string } =
    capture.status === 'success'
      ? { variant: 'ok', label: 'success' }
      : capture.errors.length > 0
        ? { variant: 'critical', label: capture.errors[0].type }
        : { variant: 'neutral', label: capture.status };

  return (
    <div className="rounded-sm border border-border">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] hover:bg-surface-2"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="font-medium">{capture.label}</span>
        <code className="text-[11px] text-fg-muted">{strategyId}</code>
        <Badge variant={capture.kind === 'primary' ? 'accent' : 'ok'}>{capture.kind}</Badge>
        <Badge variant={statusBadge.variant}>{statusBadge.label}</Badge>
        {capture.cards_returned > 0 && (
          <span className="text-[11px] text-fg-muted">{capture.cards_returned} cards</span>
        )}
        {isActive && (
          <Badge variant="warning" className="ml-auto">
            Active
          </Badge>
        )}
      </button>
      {open && (
        <div className="border-t border-border px-3 py-2">
          {capture.errors.length > 0 && (
            <Section title="Errors" defaultOpen>
              {capture.errors.map((e, i) => (
                <div key={i} className="mb-1 text-[12px] text-critical">
                  <strong>{e.type}:</strong> {e.message}
                </div>
              ))}
            </Section>
          )}
          {capture.cards_summary.length > 0 && (
            <Section title={`Collector result (${capture.cards_summary.length})`} defaultOpen>
              {capture.cards_summary.map((card, i) => (
                <div key={i} className="mb-1 text-[12px]">
                  <strong>{card.service_name || 'Card'}:</strong>{' '}
                  {card.detail || card.remaining || 'returned'}
                  {card.error_type ? <span className="ml-1 text-critical">({card.error_type})</span> : null}
                </div>
              ))}
            </Section>
          )}
          {capture.requests.length > 0 && (
            <Section title={`Requests (${capture.requests.length})`}>
              {capture.requests.map((r, i) => (
                <div key={i} className="mb-1 text-[12px]">
                  <Badge variant="neutral" className="mr-1">
                    {r.method}
                  </Badge>
                  <span className="break-all font-mono text-fg-muted">{r.url}</span>
                </div>
              ))}
            </Section>
          )}
          {capture.responses.length > 0 && (
            <Section title={`Responses (${capture.responses.length})`}>
              {capture.responses.map((r, i) => (
                <ResponseBlock key={i} response={r} />
              ))}
            </Section>
          )}
          {capture.requests.length === 0 &&
            capture.responses.length === 0 &&
            capture.errors.length === 0 && (
              <div className="text-[12px] text-fg-muted">
                No HTTP traffic captured for this strategy.
              </div>
            )}
        </div>
      )}
    </div>
  );
}
