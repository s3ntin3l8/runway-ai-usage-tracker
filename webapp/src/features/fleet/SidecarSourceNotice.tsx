// Fleet-card notice when a Mac sidecar reports a new Keychain scraper.

import { useQuery } from '@tanstack/react-query';
import { fetchSidecarSources } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';

export function SidecarSourceNotice({
  sidecarId,
  onOpen,
  osPlatform,
}: {
  sidecarId: string;
  osPlatform?: string | null;
  onOpen: () => void;
}) {
  const query = useQuery({
    queryKey: ['fleet', 'sources', sidecarId],
    queryFn: () => fetchSidecarSources(sidecarId),
    enabled: osPlatform?.startsWith('Darwin') === true && /^device-[a-f0-9]{32}$/.test(sidecarId),
    refetchInterval: 60_000,
  });
  if (osPlatform?.startsWith('Darwin') !== true || !query.data?.sources.some((source) => source.announcement))
    return null;
  return (
    <aside className="mt-3 rounded-md border border-edge bg-surface-2 p-3" aria-label="New Keychain scraper">
      <p className="text-xs font-medium">New Keychain scraper—opt in now</p>
      <p className="text-xs text-fg-muted">Review what this scraper reads and choose locally on your Mac.</p>
      <Button size="sm" variant="ghost" onClick={onOpen}>
        Review Keychain scrapers
      </Button>
    </aside>
  );
}
