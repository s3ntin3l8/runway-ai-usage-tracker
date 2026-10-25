import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/cn';

const badgeVariants = cva(
  // max-w-full + min-w-0 keep a badge from widening the box it sits in (a
  // table cell, a flex row); truncate then clips the overflow instead of
  // pushing the layout wider.
  'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium leading-4 max-w-full min-w-0 truncate',
  {
    variants: {
      variant: {
        neutral: 'bg-unknown-muted text-fg-muted',
        critical: 'bg-critical-muted text-critical',
        warning: 'bg-warning-muted text-warning',
        ok: 'bg-ok-muted text-ok',
        unlimited: 'bg-unlimited-muted text-unlimited',
        unknown: 'bg-unknown-muted text-unknown',
        accent: 'bg-accent-muted text-accent-text',
        outline: 'border border-edge-strong text-fg-muted',
      },
    },
    defaultVariants: { variant: 'neutral' },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
