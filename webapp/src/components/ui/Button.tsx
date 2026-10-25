import { cva, type VariantProps } from 'class-variance-authority';
import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/cn';

// Exported so links that should look like buttons (e.g. downloads) can reuse
// the styles without nesting a <button> inside an <a>.
export const buttonVariants = cva(
  'inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-sm font-medium whitespace-nowrap transition-colors duration-150 disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        primary: 'bg-accent text-accent-fg hover:bg-accent-hover',
        secondary: 'border border-edge bg-surface-2 text-fg hover:bg-surface-3',
        ghost: 'text-fg-muted hover:bg-surface-2 hover:text-fg',
        danger: 'bg-critical-strong text-white hover:opacity-90',
        'danger-ghost': 'text-critical hover:bg-critical-muted',
      },
      size: {
        // Touch floor: on phones every control needs a ~44px target, but the
        // desktop dashboard is deliberately dense. The min-height applies
        // below the lg breakpoint (where the bottom nav lives) and is dropped
        // again from lg up, so desktop keeps its compact rhythm.
        sm: 'h-8 min-h-11 px-2.5 text-xs lg:min-h-0',
        md: 'h-9 min-h-11 px-3 text-[13px] lg:min-h-0',
        // Mobile-friendly: meets the 44px touch-target floor
        lg: 'h-11 px-4 text-sm',
        icon: 'size-9 min-h-11 min-w-11 lg:min-h-0 lg:min-w-0',
        'icon-sm': 'size-8 min-h-11 min-w-11 lg:min-h-0 lg:min-w-0',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean;
}

export function Button({ className, variant, size, loading, children, disabled, ...props }: ButtonProps) {
  return (
    <button
      type="button"
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
      {children}
    </button>
  );
}
