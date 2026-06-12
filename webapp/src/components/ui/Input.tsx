import { cn } from '@/lib/cn';

export function Input({ className, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        'h-9 w-full rounded-sm border border-edge bg-surface-2 px-3 text-sm text-fg outline-none transition-colors duration-150',
        'placeholder:text-fg-subtle focus:border-accent disabled:opacity-50',
        className,
      )}
      {...props}
    />
  );
}

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label className={cn('text-xs font-medium text-fg-muted', className)} {...props} />
  );
}

export function HelperText({
  className,
  error,
  ...props
}: React.HTMLAttributes<HTMLParagraphElement> & { error?: boolean }) {
  return (
    <p
      className={cn('text-xs', error ? 'text-critical' : 'text-fg-subtle', className)}
      {...props}
    />
  );
}
