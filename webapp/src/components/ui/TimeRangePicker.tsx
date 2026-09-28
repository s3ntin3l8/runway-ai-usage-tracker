// Unified time-range picker: a trigger button showing the current range label
// opens a Grafana-style popover with quick ranges (Last 7/14/30/90 days, This
// month, Last month) on the left and absolute From/To date inputs on the
// right. Shared by History, Insights, and the period-aware Provider tabs;
// the selected value lives in the `?range=` URL param (see useRangeParam).

import { useEffect, useState } from 'react';
import { CalendarDays, ChevronDown } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Popover } from '@/components/ui/Popover';
import { cn } from '@/lib/cn';
import {
  formatRangeLabel,
  isRolling,
  isoDateOfInstant,
  quickRanges,
  serializeRangeParam,
  todayISODate,
  type DateRangeValue,
} from '@/lib/timeRange';

interface TimeRangePickerProps {
  value: DateRangeValue;
  onChange: (value: DateRangeValue) => void;
  /** Earliest allowed From date (an ISO instant, e.g. the first recorded event). */
  earliest?: string | null;
  className?: string;
}

function toISODate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return toISODate(d);
}

export function TimeRangePicker({ value, onChange, earliest, className }: TimeRangePickerProps) {
  const [open, setOpen] = useState(false);
  const label = formatRangeLabel(value);
  const ranges = quickRanges();
  const serialized = serializeRangeParam(value);

  const since = value.since;
  const until = value.until;
  const days = isRolling(value) ? value.days : undefined;
  const [from, setFrom] = useState(() => since ?? daysAgo(days ?? 30));
  const [to, setTo] = useState(() => until ?? todayISODate());

  useEffect(() => {
    if (since) {
      setFrom(since);
      setTo(until ?? todayISODate());
    } else {
      setFrom(daysAgo(days ?? 30));
      setTo(todayISODate());
    }
  }, [since, until, days]);

  const earliestDate = earliest ? isoDateOfInstant(earliest) : undefined;

  const applyAbsolute = () => {
    if (!from || !to) return;
    const [start, end] = from <= to ? [from, to] : [to, from];
    // Clamp to today: a future window has no data yet and the backend would
    // happily return an empty bucket (a silently blank stats page).
    const today = todayISODate();
    onChange({ since: start > today ? today : start, until: end > today ? today : end });
    setOpen(false);
  };

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      className="w-80"
      trigger={
        <Button
          variant="ghost"
          size="sm"
          className={cn('h-9 gap-1.5 px-2.5 font-medium', open && 'bg-surface-2 text-fg', className)}
        >
          <CalendarDays className="size-3.5" aria-hidden />
          <span className="max-w-44 truncate">{label}</span>
          <ChevronDown className="size-3 text-fg-muted" aria-hidden />
        </Button>
      }
    >
      <div className="flex gap-4">
        <div className="flex flex-col gap-0.5">
          <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-fg-muted">
            Quick ranges
          </p>
          {ranges.map((r) => {
            // Quick ranges re-derive their bounds from "now" on every render,
            // so the active state matches serialized spans rather than stable
            // ids: a stored month span re-labels itself at a rollover ("This
            // month" → "Last month") or shows no highlight once it matches no
            // quick range. The trigger label formats the stored value and
            // therefore never drifts.
            const active = serializeRangeParam(r.value) === serialized;
            return (
              <button
                key={r.id}
                type="button"
                aria-pressed={active}
                onClick={() => {
                  onChange(r.value);
                  setOpen(false);
                }}
                className={cn(
                  'rounded-sm px-2 py-1.5 text-left text-xs transition-colors',
                  active
                    ? 'bg-surface-2 font-medium text-fg'
                    : 'text-fg-muted hover:bg-surface-2 hover:text-fg',
                )}
              >
                {r.label}
              </button>
            );
          })}
        </div>

        <div className="flex flex-col gap-2 border-l border-edge pl-4">
          <p className="text-[11px] font-medium uppercase tracking-wide text-fg-muted">
            Absolute time range
          </p>
          <label className="flex items-center gap-2 text-[11px] text-fg-muted">
            From
            <input
              type="date"
              value={from}
              min={earliestDate}
              max={todayISODate()}
              onChange={(e) => setFrom(e.target.value)}
              className="h-8 rounded border border-edge bg-surface-1 px-2 text-xs text-fg"
            />
          </label>
          <label className="flex items-center gap-2 text-[11px] text-fg-muted">
            To
            <input
              type="date"
              value={to}
              min={from || earliestDate}
              max={todayISODate()}
              onChange={(e) => setTo(e.target.value)}
              className="h-8 rounded border border-edge bg-surface-1 px-2 text-xs text-fg"
            />
          </label>
          <Button size="sm" className="mt-1 self-end" onClick={applyAbsolute}>
            Apply
          </Button>
        </div>
      </div>
    </Popover>
  );
}
