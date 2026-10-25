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

// Keep the selected tab in view.
//
// The strip scrolls sideways on a phone and the selected tab can sit past the
// fold: the provider strip holds six tabs and two or three of them fit. A
// selection the reader cannot see is a selection they cannot trust — the
// panel shows one tab's content while the strip points at another.
//
// The scroll is computed here rather than delegated to `scrollIntoView`,
// which animates (the strip is `scroll-smooth`). An animation can be cut
// short by the layout settling underneath it, and a deep link then lands with
// the named tab still half off-screen. Assigning `scrollLeft` is immediate
// and lands where it is told to.
//
// The strip is the only thing that knows how wide its own content is, so it
// is the only place that can answer "is the selected tab on screen?".
function revealSelectedTab(strip: HTMLElement): void {
  const trigger = strip.querySelector<HTMLElement>('[data-state="active"]');
  if (!trigger) return;
  const stripBox = strip.getBoundingClientRect();
  const triggerBox = trigger.getBoundingClientRect();
  // jsdom has no layout, so both boxes are empty. Nothing to reveal.
  if (stripBox.width === 0 || triggerBox.width === 0) return;
  const offset = triggerBox.left - stripBox.left;
  // Fully inside the strip already? The reader can see it — leave the strip
  // where it is, so a scroll the reader made is not undone.
  if (offset >= 0 && offset + triggerBox.width <= stripBox.width) return;
  // Centre the selected tab. The browser clamps the scroll at the strip's
  // ends, so a tab at either end ends flush against it.
  strip.scrollLeft += offset - (stripBox.width - triggerBox.width) / 2;
}

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
    // The selected tab may start past the fold — a deep link names a tab, and
    // a tab that is not on screen is a tab the reader cannot see.
    revealSelectedTab(el);
    el.addEventListener('scroll', update, { passive: true });
    // The strip is width-constrained, so a trigger appearing or disappearing
    // (the provider strip adds "Debug" on demand) changes the scroll width
    // without resizing the strip itself.
    //
    // The triggers are observed as well as the strip. The strip's own box
    // does not change when a trigger's does, and a trigger's width can move
    // without the DOM moving at all — a web-font swap, or a re-rendered
    // label. A fade that outlives its trigger is worse than no fade: it
    // says there is more to the right when there is not.
    //
    // A resize also moves the selected tab: the strip can get narrower (a
    // scrollbar appears) or its content can get wider, and either way the
    // tab that was in view can end up clipped. So the selection is revealed
    // again after every resize.
    const sizes = new ResizeObserver(() => {
      update();
      revealSelectedTab(el);
    });
    const observeStrip = () => {
      sizes.observe(el);
      for (const trigger of el.children) sizes.observe(trigger);
    };
    observeStrip();
    // `subtree` and `characterData` catch a trigger whose own label
    // changes: the strip's scroll width moves, and an observer that only
    // watches the strip's children would not fire.
    //
    // `attributes` on `data-state` catches the selection moving: clicking a
    // tab, or landing on a deep link, has to bring the new selection into
    // view.
    const mutations = new MutationObserver(() => {
      observeStrip();
      update();
      revealSelectedTab(el);
    });
    mutations.observe(el, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ['data-state'],
    });
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
      className={cn(
        // The panel is focusable (Radix gives it tabIndex 0), and a bare
        // `outline-none` silenced the global focus ring on it. The repo's
        // own idiom replaces the outline with a visible ring.
        'pt-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        className,
      )}
      {...props}
    />
  );
}
