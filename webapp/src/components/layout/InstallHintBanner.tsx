// iOS Safari has no install API, so we show a one-time, dismissible hint on how
// to add Runway to the home screen. Android/desktop get a real install button in
// Settings → About instead (see useInstallPrompt).

import { useState } from 'react';
import { Share, X } from 'lucide-react';
import { useInstallPrompt } from '@/hooks/useInstallPrompt';

const DISMISS_KEY = 'runway:ios-install-hint-dismissed';

export function InstallHintBanner() {
  const { isIOS, isStandalone } = useInstallPrompt();
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(DISMISS_KEY) === '1');

  if (!isIOS || isStandalone || dismissed) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(DISMISS_KEY, '1');
    } catch {
      // Private-mode / quota — state still hides it for the session.
    }
    setDismissed(true);
  };

  return (
    <div className="flex items-center gap-3 border-b border-edge bg-accent-muted px-4 py-2 text-[13px] text-accent lg:px-8">
      <Share className="size-4 shrink-0" aria-hidden />
      <p className="min-w-0 flex-1">
        Install Runway: tap <span className="font-semibold">Share</span> then{' '}
        <span className="font-semibold">Add to Home Screen</span>.
      </p>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss install hint"
        className="shrink-0 rounded-sm p-1 hover:bg-accent/10"
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
