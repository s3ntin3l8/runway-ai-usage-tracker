import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden
      className={cn('rounded-sm bg-surface-3 shimmer-bg animate-shimmer', className)}
      {...props}
    />
  );
}

/**
 * A region that is still loading.
 *
 * The shimmer itself is decorative (`aria-hidden`), so a screen-reader user
 * hears nothing while the data is on the way — and, because the region is
 * replaced rather than updated, nothing when it arrives either. The repo's
 * convention for a loading region is `role="status"`; this adds the busy
 * state and keeps the words out of the visual layout.
 */
export function SkeletonRegion({
  className,
  label = 'Loading',
  children,
}: {
  className?: string;
  label?: string;
  children: ReactNode;
}) {
  return (
    <div role="status" aria-busy="true" className={className}>
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}
