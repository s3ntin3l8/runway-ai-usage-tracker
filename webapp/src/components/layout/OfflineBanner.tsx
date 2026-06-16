// Slim, non-dismissible banner shown while the browser is offline. The SPA
// shell still loads from the service-worker cache, but live data needs the
// network — this makes the stale state explicit. Auto-hides on reconnect.

import { WifiOff } from 'lucide-react';
import { useOnlineStatus } from '@/hooks/useOnlineStatus';

export function OfflineBanner() {
  const online = useOnlineStatus();
  if (online) return null;

  return (
    <div
      role="status"
      className="flex items-center gap-3 border-b border-edge bg-surface-2 px-4 py-2 text-[13px] text-fg-muted lg:px-8"
    >
      <WifiOff className="size-4 shrink-0" aria-hidden />
      <p className="min-w-0 flex-1">
        You&rsquo;re offline — showing the last loaded data. Reconnecting automatically.
      </p>
    </div>
  );
}
