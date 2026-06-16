// Shared time-scope control for the provider data tabs. A Month ⟷ Rolling
// toggle picks the scope mode; the contextual control then either steps through
// calendar months (prev/next capped at the earliest event and the current
// month) or selects a rolling 7/14/30/90-day window. Whatever it emits is the
// single `?period=` value every period-aware tab honours together (issue #87).

import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { formatLocalDate, startOfMonthISO } from '@/lib/tz';
import {
  DEFAULT_ROLLING_DAYS,
  ROLLING_DAYS,
  currentMonthKey,
  isRollingKey,
  monthKeyOfISO,
  resolvePeriod,
  rollingKey,
  shiftMonthKey,
  type ScopeMode,
} from './period';

export function ScopeSelector({
  value,
  mode,
  onChange,
  earliest,
}: {
  value: string; // 'YYYY-MM' or 'Nd'
  mode: ScopeMode;
  onChange: (next: string) => void;
  earliest: string | null | undefined;
}) {
  // Switching modes resets to that mode's default: the current month, or the
  // default rolling window. Re-selecting the active mode is a no-op.
  const onMode = (next: string) => {
    if (next === mode) return;
    onChange(next === 'rolling' ? rollingKey(DEFAULT_ROLLING_DAYS) : currentMonthKey());
  };

  return (
    <div className="flex items-center gap-2" role="group" aria-label="Select time scope">
      <Tabs value={mode} onValueChange={onMode}>
        <TabsList className="border-0">
          <TabsTrigger value="month" className="h-8 px-2.5">
            Month
          </TabsTrigger>
          <TabsTrigger value="rolling" className="h-8 px-2.5">
            Rolling
          </TabsTrigger>
        </TabsList>
      </Tabs>
      {mode === 'rolling' ? (
        <RollingControl value={value} onChange={onChange} />
      ) : (
        <MonthStepper value={value} onChange={onChange} earliest={earliest} />
      )}
    </div>
  );
}

function RollingControl({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  // Fall back to the default window if the URL carries an unknown rolling key.
  const active = isRollingKey(value) ? value : rollingKey(DEFAULT_ROLLING_DAYS);
  return (
    <Tabs value={active} onValueChange={onChange}>
      <TabsList className="border-0">
        {ROLLING_DAYS.map((d) => (
          <TabsTrigger key={d} value={rollingKey(d)} className="h-8 px-2">
            {d}d
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}

function MonthStepper({
  value,
  onChange,
  earliest,
}: {
  value: string;
  onChange: (next: string) => void;
  earliest: string | null | undefined;
}) {
  const { year, month, isCurrentMonth } = resolvePeriod(value);
  const label = formatLocalDate(startOfMonthISO(year, month), { month: 'long', year: 'numeric' });

  const earliestKey = monthKeyOfISO(earliest);
  const prevKey = shiftMonthKey(value, -1);
  const canPrev = !earliestKey || prevKey >= earliestKey;

  return (
    <div className="flex items-center gap-1" role="group" aria-label="Select month">
      <Button
        size="icon-sm"
        variant="ghost"
        disabled={!canPrev}
        onClick={() => onChange(prevKey)}
        aria-label="Previous month"
        title="Previous month"
      >
        <ChevronLeft className="size-3.5" aria-hidden />
      </Button>
      <span className="min-w-32 text-center text-xs font-medium tabular" aria-live="polite">
        {label}
      </span>
      <Button
        size="icon-sm"
        variant="ghost"
        disabled={isCurrentMonth}
        onClick={() => onChange(shiftMonthKey(value, 1))}
        aria-label="Next month"
        title={isCurrentMonth ? 'Already at the current month' : 'Next month'}
      >
        <ChevronRight className="size-3.5" aria-hidden />
      </Button>
      {!isCurrentMonth ? (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => onChange(currentMonthKey())}
          className="text-[11px]"
        >
          Today
        </Button>
      ) : null}
    </div>
  );
}
