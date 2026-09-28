// `?range=` URL sync for the shared time-range picker. The param holds 'Nd'
// for rolling windows, 'YYYY-MM-DD_YYYY-MM-DD' for absolute spans; omitted
// means the default last-7-days window (see lib/timeRange). `fallbackKey`
// lets a page (the Provider detail) keep honouring a legacy param — it is
// read when `range` is absent and cleared whenever a new range is set.

import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router';
import {
  parseRangeParam,
  serializeRangeParam,
  type DateRangeValue,
} from '@/lib/timeRange';

export function useRangeParam(fallbackKey?: string): readonly [DateRangeValue, (value: DateRangeValue) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get('range') ?? (fallbackKey ? searchParams.get(fallbackKey) : null);
  const value = useMemo(() => parseRangeParam(raw), [raw]);

  const setRange = useCallback(
    (next: DateRangeValue) => {
      setSearchParams(
        (prev) => {
          const p = new URLSearchParams(prev);
          const serialized = serializeRangeParam(next);
          if (serialized) p.set('range', serialized);
          else p.delete('range');
          if (fallbackKey) p.delete(fallbackKey);
          return p;
        },
        { replace: true },
      );
    },
    [setSearchParams, fallbackKey],
  );

  return [value, setRange] as const;
}
