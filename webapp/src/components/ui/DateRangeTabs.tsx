// Date range selector: preset tabs (7d/14d/30d/90d) plus a calendar icon
// button that opens a Popover with native date inputs for custom range.

import { useEffect, useState } from 'react';
import { CalendarDays } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Popover } from '@/components/ui/Popover';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { cn } from '@/lib/cn';

const PRESETS = [
  { days: 7, label: '7d' },
  { days: 14, label: '14d' },
  { days: 30, label: '30d' },
  { days: 90, label: '90d' },
];

export interface DateRangeValue {
  days?: number; // preset mode
  since?: string; // ISO-8601 date (custom mode)
  until?: string; // ISO-8601 date (custom mode)
}

interface DateRangeTabsProps {
  value: DateRangeValue;
  onChange: (range: DateRangeValue) => void;
  className?: string;
}

function toISODate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function daysAgo(n: number): Date {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d;
}

export function DateRangeTabs({ value, onChange, className }: DateRangeTabsProps) {
  const [open, setOpen] = useState(false);
  const isPreset = value.days != null;

  const [customFrom, setCustomFrom] = useState(() =>
    value.since ? value.since.slice(0, 10) : toISODate(daysAgo(30)),
  );
  const [customTo, setCustomTo] = useState(() =>
    value.until ? value.until.slice(0, 10) : toISODate(new Date()),
  );

  useEffect(() => {
    if (value.since) setCustomFrom(value.since.slice(0, 10));
    if (value.until) setCustomTo(value.until.slice(0, 10));
  }, [value.since, value.until]);

  const applyCustom = () => {
    if (!customFrom || !customTo) return;
    const [from, to] = customFrom <= customTo
      ? [customFrom, customTo]
      : [customTo, customFrom];
    onChange({ since: from, until: to });
    setOpen(false);
  };

  const calendarLabel = !isPreset
    ? `${customFrom.slice(5)} – ${customTo.slice(5)}`
    : undefined;

  return (
    <div className={cn('flex items-center gap-1', className)}>
      <Tabs
        value={isPreset ? String(value.days) : ''}
        onValueChange={(v) => {
          if (v) onChange({ days: Number(v) });
        }}
      >
        <TabsList className="border-0">
          {PRESETS.map((r) => (
            <TabsTrigger key={r.days} value={String(r.days)} className="h-9 px-2.5">
              {r.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <Popover
        open={open}
        onOpenChange={setOpen}
        trigger={
          <Button
            variant="ghost"
            size="sm"
            className={cn(
              'h-9 gap-1.5 px-2.5',
              !isPreset && 'bg-surface-2 text-fg',
            )}
          >
            <CalendarDays className="size-3.5" />
            {calendarLabel && (
              <span className="max-w-[6rem] truncate text-[11px]">{calendarLabel}</span>
            )}
          </Button>
        }
      >
        <div className="flex flex-col gap-2">
          <p className="text-xs font-medium text-fg">Custom range</p>
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-fg-muted">From</label>
            <input
              type="date"
              value={customFrom}
              onChange={(e) => setCustomFrom(e.target.value)}
              className="h-8 rounded border border-edge bg-surface-1 px-2 text-xs text-fg"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-fg-muted">To</label>
            <input
              type="date"
              value={customTo}
              onChange={(e) => setCustomTo(e.target.value)}
              className="h-8 rounded border border-edge bg-surface-1 px-2 text-xs text-fg"
            />
          </div>
          <Button size="sm" className="mt-1 self-end" onClick={applyCustom}>
            Apply
          </Button>
        </div>
      </Popover>
    </div>
  );
}
