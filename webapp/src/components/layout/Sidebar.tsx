import { Link, useLocation } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { fetchSettings } from '@/api/endpoints';
import { cn } from '@/lib/cn';
import { Badge } from '@/components/ui/Badge';
import { Tooltip } from '@/components/ui/Tooltip';
import { useFleetUpdateCount } from '@/features/fleet/queries';
import { NAV_ITEMS, isNavItemActive } from './nav';
import { prefetchRoute } from './routePrefetch';
import { RunwayMark } from './RunwayMark';

// Updating the server means pulling a new image — link to the GitHub releases
// page, mirroring UpdateBanner.
const RELEASES_URL = 'https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases';

export function Sidebar() {
  // Reuses the cached settings query (primed at boot — no extra request). The
  // whole aside is desktop-only (hidden lg:flex), so this footer is too.
  const { data } = useQuery({ queryKey: ['system', 'settings'], queryFn: fetchSettings });
  const version = data?.version;
  const updateAvailable = data?.update_available && data?.latest_version;
  // Reuses the shared ['fleet','sidecars'] cache — no extra request.
  const fleetUpdates = useFleetUpdateCount();
  const location = useLocation();
  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-edge bg-surface-1 lg:flex">
      <div className="flex h-14 items-center gap-2.5 px-4">
        <RunwayMark className="size-6" />
        <span className="text-[15px] font-semibold tracking-tight">Runway</span>
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 px-2 pt-2" aria-label="Primary">
        {NAV_ITEMS.map((item) => {
          const active = isNavItemActive(item, location.pathname);
          return (
            <Link
              key={item.to}
              to={item.to}
              onMouseEnter={() => prefetchRoute(item.to)}
              onFocus={() => prefetchRoute(item.to)}
              aria-current={active ? 'page' : undefined}
              className={cn(
                'flex h-9 items-center gap-2.5 rounded-sm px-2.5 text-[13px] font-medium transition-colors duration-150',
                active
                  ? 'bg-surface-3 text-fg'
                  : 'text-fg-muted hover:bg-surface-2 hover:text-fg',
              )}
            >
              <item.icon className="size-4 shrink-0" aria-hidden />
              {item.label}
              {item.to === '/fleet' && fleetUpdates > 0 ? (
                <Badge
                  variant="warning"
                  className="ml-auto"
                  title={`${fleetUpdates} sidecar update${fleetUpdates === 1 ? '' : 's'} available`}
                  aria-label={`${fleetUpdates} sidecar update${fleetUpdates === 1 ? '' : 's'} available`}
                >
                  {fleetUpdates}
                </Badge>
              ) : null}
            </Link>
          );
        })}
      </nav>
      {version ? (
        <div className="flex items-center gap-1.5 px-4 py-3 text-[11px] text-fg-subtle">
          <span>v{version}</span>
          {updateAvailable ? (
                <Tooltip content={`Runway v${data!.latest_version} is available`}>
                <a href={RELEASES_URL} target="_blank" rel="noreferrer">
                  <Badge variant="warning">update</Badge>
                </a>
              </Tooltip>
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}
