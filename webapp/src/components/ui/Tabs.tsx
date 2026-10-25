import { useCallback, useEffect, useRef, useState } from 'react';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { cn } from '@/lib/cn';

export const Tabs = TabsPrimitive.Root;

// Right-edge fade: the strip scrolls horizontally on narrow screens, and with
// six provider tabs there is no sign that more of them sit off-screen. The
// mask fades the last ~24px of the strip out; it is applied only while
// something is actually scrolled off the right edge, so a strip that fits is
// never dimmed.
const SCROLL_FADE =
  '[mask-image:linear-gradient(to_right,black_calc(100%_-_24px),transparent_100%)]';

export function TabsList({
  className,
  ref,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const [canScrollRight, setCanScrollRight] = useState(false);

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const update = () => {
      const remaining = el.scrollWidth - el.clientWidth - el.scrollLeft;
      setCanScrollRight(remaining > 1);
    };
    update();
    el.addEventListener('scroll', update, { passive: true });
    // The strip is width-constrained, so a trigger appearing or disappearing
    // (the provider strip adds "Debug" on demand) changes the scroll width
    // without resizing the strip itself.
    const mutations = new MutationObserver(update);
    mutations.observe(el, { childList: true });
    const sizes = new ResizeObserver(update);
    sizes.observe(el);
    return () => {
      el.removeEventListener('scroll', update);
      mutations.disconnect();
      sizes.disconnect();
    };
  }, []);

  const setRef = useCallback(
    (node: HTMLDivElement | null) => {
      listRef.current = node;
      if (typeof ref === 'function') ref(node);
      else if (ref) ref.current = node;
    },
    [ref],
  );

  return (
    <TabsPrimitive.List
      {...props}
      ref={setRef}
      className={cn(
        // Horizontal scroll on narrow screens instead of wrapping/cramping.
        // Pin overflow-y: setting overflow-x alone computes overflow-y to auto,
        // which the triggers' -mb-px/border-b-2 turns into a stray scrollbar.
        'flex w-full items-center gap-1 overflow-x-auto overflow-y-hidden border-b border-edge scroll-smooth',
        canScrollRight && SCROLL_FADE,
        className,
      )}
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
        // 44px on phones (the 7-tab provider strip is a touch target), the
        // compact 40px from lg up.
        'relative -mb-px flex h-11 min-w-fit cursor-pointer items-center gap-1.5 border-b-2 border-transparent px-3 text-[13px] font-medium text-fg-muted transition-colors duration-150 lg:h-10',
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
