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
