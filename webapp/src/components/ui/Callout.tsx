import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

const TONES = {
  info: 'bg-accent-muted text-accent-text',
  warning: 'bg-warning-muted text-warning',
  critical: 'bg-critical-muted text-critical',
} as const;

/** A tinted inline message with an optional action: one shape for every notice inside a card. */
export function Callout({
  tone = 'info',
  icon,
  action,
  children,
  className,
  ...rest
}: {
  tone?: keyof typeof TONES;
  icon?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
} & Pick<React.HTMLAttributes<HTMLDivElement>, 'role'>) {
  return (
    <div
      className={cn('flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md px-3 py-2 text-[12px] leading-5', TONES[tone], className)}
      {...rest}
    >
      {icon ? (
        <span aria-hidden className="shrink-0">
          {icon}
        </span>
      ) : null}
      <div className="min-w-[14rem] flex-1">{children}</div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}
