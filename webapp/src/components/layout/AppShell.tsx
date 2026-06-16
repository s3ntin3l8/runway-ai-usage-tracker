import { Outlet } from 'react-router';
import { Sidebar } from './Sidebar';
import { BottomNav } from './BottomNav';
import { UpdateBanner } from './UpdateBanner';
import { OfflineBanner } from './OfflineBanner';
import { InstallHintBanner } from './InstallHintBanner';

export function AppShell() {
  return (
    <div className="min-h-dvh">
      <Sidebar />
      <main className="pb-20 lg:pb-0 lg:pl-56">
        <UpdateBanner />
        <OfflineBanner />
        <InstallHintBanner />
        <Outlet />
      </main>
      <BottomNav />
    </div>
  );
}
