import { Outlet } from 'react-router';
import PullToRefresh from 'react-simple-pull-to-refresh';
import { useForceCollect } from '@/hooks/useForceCollect';
import { useIsDesktop } from '@/hooks/useMediaQuery';
import { usePullToRefreshSuspended } from '@/lib/pullToRefresh';
import { Sidebar } from './Sidebar';
import { BottomNav } from './BottomNav';
import { UpdateBanner } from './UpdateBanner';
import { OfflineBanner } from './OfflineBanner';
import { InstallHintBanner } from './InstallHintBanner';
import { PullingIndicator, RefreshingIndicator } from './PullRefreshIndicator';

export function AppShell() {
  // Same "force collect" mutation the Home page's manual button uses (useForceCollect) —
  // pull-to-refresh is just a touch-gesture entry point to the same server-side collect.
  const collect = useForceCollect();
  // Touch-only: the library also binds mouse drag, which would make an accidental
  // click-drag at the top of a desktop page trigger a collect. Desktop keeps the button.
  const isDesktop = useIsDesktop();
  // A nested dnd-kit drag (e.g. Home's provider grid reorder) reports itself
  // here for the duration of its own drag — see lib/pullToRefresh.ts for why.
  const dragSuspended = usePullToRefreshSuspended();

  return (
    <div className="min-h-dvh">
      {/* Skip link (WCAG 2.4.1, Bypass Blocks). Without it a keyboard user
          has to tab through the whole primary nav on every page load before
          reaching any content. It stays in the layout until it takes focus. */}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:rounded-sm focus:bg-surface-2 focus:px-3 focus:py-2 focus:text-sm focus:text-fg focus:shadow-md"
      >
        Skip to main content
      </a>
      <Sidebar />
      {/* The bottom padding has to cover the whole bottom nav, which is h-16
          PLUS the home-indicator inset on a notched iPhone (≈34px). The old
          fixed pb-20 left the last ~18px of every page under the nav. The app
          scrolls at window level, so this is the only reserve. */}
      <main id="main" tabIndex={-1} className="pb-[calc(5rem+env(safe-area-inset-bottom))] lg:pb-0 lg:pl-56">
        <UpdateBanner />
        <OfflineBanner />
        <InstallHintBanner />
        <PullToRefresh
          isPullable={!isDesktop && !dragSuspended}
          onRefresh={collect.mutateAsync}
          pullingContent={<PullingIndicator />}
          refreshingContent={<RefreshingIndicator />}
        >
          <Outlet />
        </PullToRefresh>
      </main>
      <BottomNav />
    </div>
  );
}
