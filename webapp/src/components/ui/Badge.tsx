import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/cn';

const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium leading-4',
  {
    variants: {
      variant: {
        neutral: 'bg-unknown-muted text-fg-muted',
        critical: 'bg-critical-muted text-critical',
        warning: 'bg-warning-muted text-warning',
        ok: 'bg-ok-muted text-ok',
        unlimited: 'bg-unlimited-muted text-unlimited',
        unknown: 'bg-unknown-muted text-unknown',
        accent: 'bg-accent-muted text-accent',
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
