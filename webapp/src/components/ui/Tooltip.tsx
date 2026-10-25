import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import { cn } from '@/lib/cn';

export const TooltipProvider = TooltipPrimitive.Provider;

interface TooltipProps {
  content: React.ReactNode;
  children: React.ReactNode;
  side?: 'top' | 'bottom' | 'left' | 'right';
  className?: string;
}

export function Tooltip({ content, children, side = 'top', className }: TooltipProps) {
  return (
    <TooltipPrimitive.Root delayDuration={250}>
      {/*
        Radix opens the tooltip on the trigger's focus, so the trigger has to
        be focusable. The app wraps role-less markup here (status badges, a
        <p>), which has no tab stop — the hint was hover-only and unreachable
        by keyboard and screen reader. The host span adds that tab stop.
        'inline' keeps the wrapper from changing the child's layout (a long
        collection path still wraps the way it did before).
      */}
      <TooltipPrimitive.Trigger asChild>
        <span tabIndex={0} className="inline">
          {children}
        </span>
      </TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className={cn(
            'z-50 max-w-xs rounded-sm border border-edge bg-overlay px-2.5 py-1.5 text-xs text-fg shadow-md',
            className,
          )}
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
