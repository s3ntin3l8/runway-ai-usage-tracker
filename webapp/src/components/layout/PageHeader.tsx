import { cn } from '@/lib/cn';

interface PageHeaderProps {
  /** @deprecated Prefer children for custom layouts; retained for existing page headers. */
  title?: string;
  /** @deprecated Prefer children for custom layouts; retained for existing page headers. */
  description?: string;
  /** @deprecated Prefer children for custom layouts; retained for existing page headers. */
  leading?: React.ReactNode;
  /** @deprecated Prefer children for custom layouts; retained for existing page headers. */
  actions?: React.ReactNode;
  className?: string;
  sticky?: boolean;
  children?: React.ReactNode;
}

export function PageHeader({
  title,
  description,
  leading,
  actions,
  className,
  sticky = true,
  children,
}: PageHeaderProps) {
  return (
    <header
      className={cn(
        // The optional position lets pages keep the shared header structure
        // while opting out of pinning when embedded in a scroll region.
        // pt-[env(safe-area-inset-top)] covers the notch in standalone iOS.
        cn(
          'flex h-14 items-center justify-between gap-3 border-b border-edge bg-canvas/90 px-4 pt-[env(safe-area-inset-top)] backdrop-blur-sm lg:px-8',
          sticky && 'sticky top-0 z-20',
        ),
        className,
      )}
    >
      {children ?? (title ? (
        <>
          <div className="flex min-w-0 items-center gap-3">
            {leading}
            <div className="min-w-0">
              <h1 className="truncate text-[15px] font-semibold tracking-tight">{title}</h1>
              {description ? (
                <p className="truncate text-xs text-fg-subtle">{description}</p>
              ) : null}
            </div>
          </div>
          {actions ? (
            <div className="min-w-0 max-w-[72vw] shrink-0 overflow-x-auto lg:max-w-none">
              <div className="flex w-max items-center gap-2">{actions}</div>
            </div>
          ) : null}
        </>
      ) : null)}
    </header>
  );
}
