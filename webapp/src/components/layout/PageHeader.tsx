import { cn } from '@/lib/cn';

interface PageHeaderProps {
  title: string;
  description?: string;
  leading?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}

export function PageHeader({ title, description, leading, actions, className }: PageHeaderProps) {
  return (
    <header
      className={cn(
        'sticky top-0 z-20 flex h-14 items-center justify-between gap-3 border-b border-edge bg-canvas/90 px-4 backdrop-blur-sm lg:px-8',
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        {leading}
        <div className="min-w-0">
          <h1 className="truncate text-[15px] font-semibold tracking-tight">{title}</h1>
          {description ? (
            <p className="truncate text-xs text-fg-subtle">{description}</p>
          ) : null}
        </div>
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}
