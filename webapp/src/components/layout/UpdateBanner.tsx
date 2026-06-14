// Slim, dismissible banner shown across all routes when a newer Runway server
// release is published. Notify-only: the SPA is baked into the server image, so
// updating means pulling a new image — we link to the GitHub releases page.

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowUpCircle, X } from 'lucide-react';
import { fetchSettings } from '@/api/endpoints';

const RELEASES_URL = 'https://github.com/s3ntin3l8/runway/releases';
const DISMISS_PREFIX = 'runway:update-dismissed:';

export function UpdateBanner() {
  // Reuses the cached settings query (BootGate primes it at boot — no new request).
  const { data } = useQuery({ queryKey: ['system', 'settings'], queryFn: fetchSettings });
  // Bumped on dismiss to re-read localStorage; avoids a mount-time initializer
  // that would run before the settings query resolves (latest still null).
  const [, force] = useState(0);

  const latest = data?.latest_version ?? null;
  if (!data?.update_available || !latest) return null;

  // Keyed by version so a *newer* release re-shows even after a prior dismiss.
  const dismissKey = `${DISMISS_PREFIX}${latest}`;
  if (localStorage.getItem(dismissKey) === '1') return null;

  const dismiss = () => {
    try {
      localStorage.setItem(dismissKey, '1');
    } catch {
      // Private-mode / quota — re-render still hides it for the session.
    }
    force((n) => n + 1);
  };

  return (
    <div className="flex items-center gap-3 border-b border-edge bg-warning-muted px-4 py-2 text-[13px] text-warning lg:px-8">
      <ArrowUpCircle className="size-4 shrink-0" aria-hidden />
      <p className="min-w-0 flex-1">
        Runway <span className="font-semibold">v{latest}</span> is available — pull the latest
        server image to update.{' '}
        <a
          href={RELEASES_URL}
          target="_blank"
          rel="noreferrer"
          className="font-medium underline underline-offset-2"
        >
          Release notes
        </a>
      </p>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss update notification"
        className="shrink-0 rounded-sm p-1 hover:bg-warning/10"
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
