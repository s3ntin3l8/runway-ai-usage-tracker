import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import { cn } from '@/lib/cn';
import { rendersFocusableElement } from '@/lib/focusable';

export const TooltipProvider = TooltipPrimitive.Provider;

interface TooltipProps {
  content: React.ReactNode;
  children: React.ReactNode;
  side?: 'top' | 'bottom' | 'left' | 'right';
  className?: string;
}

export function Tooltip({ content, children, side = 'top', className }: TooltipProps) {
  // Radix opens the tooltip on the trigger's focus, so the trigger has to be
  // focusable. The app wraps role-less markup here (status badges, a text
  // line), which has no tab stop — the hint was hover-only and unreachable by
  // keyboard and screen reader, so those get a focusable host.
  //
  // A child that is ALREADY focusable must not get one: a link or a button
  // wrapped in a tabbable span becomes two tab stops for one control.
  const needsHost = !rendersFocusableElement(children);
  return (
    <TooltipPrimitive.Root delayDuration={250}>
      <TooltipPrimitive.Trigger asChild>
        {needsHost ? (
          // 'inline' keeps the wrapper from changing the child's layout (a
          // long collection path still wraps the way it did before).
          <span tabIndex={0} className="inline">
            {children}
          </span>
        ) : (
          children
        )}
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
