import { useEffect, useState } from 'react';
import { timeUntil } from '@/lib/format';
import { cn } from '@/lib/cn';

interface CountdownProps {
  // ISO timestamp of the reset boundary
  until: string | null | undefined;
  className?: string;
  prefix?: string;
}

// Live "resets in 4h 12m" label, ticking once a minute (the display
// granularity — finer intervals would be wasted renders).
export function Countdown({ until, className, prefix = 'resets in' }: CountdownProps) {
  const [, tick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  const remaining = timeUntil(until);
  if (!remaining) return null;
  return (
    // role="timer" says "this is a clock" without announcing every tick —
    // a live region here would interrupt a screen-reader user once a minute.
    <span
      role="timer"
      className={cn('font-mono text-xs text-fg-muted tabular', className)}
    >
      {prefix} {remaining}
    </span>
  );
}
