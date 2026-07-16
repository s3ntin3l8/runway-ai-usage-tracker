// Themed pull/refresh indicators for the app-wide pull-to-refresh gesture
// (see AppShell). react-simple-pull-to-refresh renders these in place of its
// unstyled defaults while the user is dragging or the collect is in flight.

import type { ReactNode } from 'react';
import { Loader2, RefreshCw } from 'lucide-react';

function IndicatorRow({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-center justify-center gap-2 py-3 text-[13px] text-fg-muted">
      {icon}
      {children}
    </div>
  );
}

export function PullingIndicator() {
  return (
    <IndicatorRow icon={<RefreshCw className="size-4" aria-hidden />}>
      Pull to refresh
    </IndicatorRow>
  );
}

export function RefreshingIndicator() {
  return (
    <IndicatorRow icon={<Loader2 className="size-4 animate-spin" aria-hidden />}>
      Refreshing…
    </IndicatorRow>
  );
}
