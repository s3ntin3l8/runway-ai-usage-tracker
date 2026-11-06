// Debug: composition root. Always shows the collection-context pane and the
// credential-sources pane (from the credential inventory), plus an on-demand
// capture of raw upstream collector responses (admin-gated, live HTTP calls —
// never auto-fetches).

import type { FleetEntry } from '@/api/types';
import { CollectionContextPane } from './CollectionContextPane';
import { CredentialSourcesPane } from './CredentialSourcesPane';
import { RawCapturePane } from './RawCapturePane';

export function DebugTab({
  providerId,
  accountId,
  entry,
  active,
}: {
  providerId: string;
  accountId: string;
  entry: FleetEntry;
  active: boolean;
}) {
  const g = entry.critical_gauge;
  // Local event cards can be synthesized for registered quota providers when
  // their server collector has not returned a quota card yet. Use capability
  // from the fleet registry when available; fall back for older API responses.
  const captureSupported = entry.server_collector_available ?? g.data_source !== 'local';

  return (
    <div className="flex flex-col gap-4">
      <CollectionContextPane providerId={providerId} accountId={accountId} entry={entry} />
      <CredentialSourcesPane
        key={`${providerId}:${accountId}`}
        providerId={providerId}
        accountId={accountId}
      />
      <RawCapturePane
        providerId={providerId}
        accountId={accountId}
        active={active}
        captureSupported={captureSupported}
      />
    </div>
  );
}
