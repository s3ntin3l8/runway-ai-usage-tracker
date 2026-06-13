import * as TabsPrimitive from '@radix-ui/react-tabs';
import { cn } from '@/lib/cn';

export const Tabs = TabsPrimitive.Root;

export function TabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn(
        // Horizontal scroll on narrow screens instead of wrapping/cramping.
        // Pin overflow-y: setting overflow-x alone computes overflow-y to auto,
        // which the triggers' -mb-px/border-b-2 turns into a stray scrollbar.
        'flex w-full items-center gap-1 overflow-x-auto overflow-y-hidden border-b border-edge',
        className,
      )}
      {...props}
    />
  );
}

export function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        'relative -mb-px flex h-10 min-w-fit cursor-pointer items-center gap-1.5 border-b-2 border-transparent px-3 text-[13px] font-medium text-fg-muted transition-colors duration-150',
        'hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg',
        className,
      )}
      {...props}
    />
  );
}

export function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      className={cn('pt-4 outline-none', className)}
      {...props}
    />
  );
}
