// Flags a paired device that is online but has never completed a source sync.
//
// A paired sidecar reports its per-source consent with `POST /api/v1/fleet/source-sync`. A
// forward-auth proxy that redirects that route to its login page breaks it silently: ingest keeps
// working, but a Mac then withholds its Keychain-derived credentials and no pause or revoke
// command reaches any enrolled host. The server reports `never` only after a grace period, and
// only for paired devices, so a legacy sidecar (null) never shows this. `stale` is the same
// failure arriving later: the device synced once and then stopped (a proxy change).

import type { Sidecar } from '@/api/types';
import { timeAgo } from '@/lib/format';

const VERIFY_URL =
  'https://github.com/s3ntin3l8/runway-ai-usage-tracker/blob/main/docs/forward-auth.md#verify-the-bypass';

export function SourceSyncNotice({ sidecar, online }: { sidecar: Sidecar; online: boolean }) {
  const state = sidecar.source_sync_state;
  if (!online || (state !== 'never' && state !== 'stale')) return null;
  return (
    <aside
      className="mt-3 rounded-md border border-edge bg-surface-2 p-3"
      aria-label="Source sync missing"
    >
      <p className="text-xs font-medium">
        {state === 'stale'
          ? `This device last synced its sources ${timeAgo(sidecar.source_sync_last_at)}`
          : 'This device has not synced its sources'}
      </p>
      <p className="text-xs text-fg-muted">
        Check that your proxy lets <code>POST /api/v1/fleet/source-sync</code> through.{' '}
        {state === 'stale'
          ? 'Until it does, this device cannot report changes to its sources or receive pause and revoke commands.'
          : 'Until it does, this device keeps its Keychain credentials to itself and cannot receive pause or revoke commands.'}{' '}
        <a
          href={VERIFY_URL}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-accent-text hover:underline"
        >
          How to verify the bypass
        </a>
      </p>
    </aside>
  );
}
