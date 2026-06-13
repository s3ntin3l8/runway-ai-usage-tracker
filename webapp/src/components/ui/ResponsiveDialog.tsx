// One modal abstraction: Radix Dialog on desktop, vaul bottom sheet on
// mobile (per the first-class-mobile requirement — sheets, not shrunk
// modals). Controlled via open/onOpenChange.

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { Drawer } from 'vaul';
import { useIsDesktop } from '@/hooks/useMediaQuery';
import { cn } from '@/lib/cn';

interface ResponsiveDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: React.ReactNode;
  // Desktop max width class, e.g. "max-w-lg"
  width?: string;
}

export function ResponsiveDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  width = 'max-w-lg',
}: ResponsiveDialogProps) {
  const isDesktop = useIsDesktop();

  if (isDesktop) {
    return (
      <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-scrim" />
          <DialogPrimitive.Content
            className={cn(
              'fixed top-1/2 left-1/2 z-50 w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-edge bg-overlay p-5 shadow-xl outline-none',
              width,
            )}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <DialogPrimitive.Title className="text-[15px] font-semibold tracking-tight">
                  {title}
                </DialogPrimitive.Title>
                {description ? (
                  <DialogPrimitive.Description className="mt-0.5 text-xs text-fg-muted">
                    {description}
                  </DialogPrimitive.Description>
                ) : null}
              </div>
              <DialogPrimitive.Close
                aria-label="Close"
                className="-m-1 cursor-pointer rounded-sm p-1 text-fg-subtle transition-colors duration-150 hover:bg-surface-2 hover:text-fg"
              >
                <X className="size-4" />
              </DialogPrimitive.Close>
            </div>
            <div className="mt-4">{children}</div>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    );
  }

  return (
    <Drawer.Root open={open} onOpenChange={onOpenChange}>
      <Drawer.Portal>
        <Drawer.Overlay className="fixed inset-0 z-40 bg-scrim" />
        <Drawer.Content className="fixed inset-x-0 bottom-0 z-50 flex max-h-[92dvh] flex-col rounded-t-lg border-t border-edge bg-overlay pb-[env(safe-area-inset-bottom)] outline-none">
          <div className="mx-auto mt-2.5 h-1 w-9 shrink-0 rounded-full bg-edge-strong" aria-hidden />
          <div className="px-4 pt-3">
            <Drawer.Title className="text-[15px] font-semibold tracking-tight">
              {title}
            </Drawer.Title>
            {description ? (
              <Drawer.Description className="mt-0.5 text-xs text-fg-muted">
                {description}
              </Drawer.Description>
            ) : null}
          </div>
          <div className="overflow-y-auto p-4">{children}</div>
        </Drawer.Content>
      </Drawer.Portal>
    </Drawer.Root>
  );
}
