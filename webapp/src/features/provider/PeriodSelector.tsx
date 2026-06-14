// Shared month stepper for the provider detail data tabs. Prev/next walk the
// `?period=YYYY-MM` selection; next is capped at the current month (no future
// data) and prev is capped at the month of the earliest recorded event.

import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { formatLocalDate, startOfMonthISO } from '@/lib/tz';
import { currentMonthKey, monthKeyOfISO, resolvePeriod, shiftMonthKey } from './period';

export function PeriodSelector({
  value,
  onChange,
  earliest,
}: {
  value: string; // 'YYYY-MM'
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
