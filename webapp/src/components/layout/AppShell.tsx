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
      <Sidebar />
      <main className="pb-20 lg:pb-0 lg:pl-56">
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
