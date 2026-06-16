import { NavLink } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { fetchSettings } from '@/api/endpoints';
import { cn } from '@/lib/cn';
import { Badge } from '@/components/ui/Badge';
import { Tooltip, TooltipProvider } from '@/components/ui/Tooltip';
import { NAV_ITEMS } from './nav';
import { RunwayMark } from './RunwayMark';

// Updating the server means pulling a new image — link to the GitHub releases
// page, mirroring UpdateBanner.
const RELEASES_URL = 'https://github.com/s3ntin3l8/runway/releases';

export function Sidebar() {
  // Reuses the cached settings query (primed at boot — no extra request). The
  // whole aside is desktop-only (hidden lg:flex), so this footer is too.
  const { data } = useQuery({ queryKey: ['system', 'settings'], queryFn: fetchSettings });
  const version = data?.version;
  const updateAvailable = data?.update_available && data?.latest_version;
  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-edge bg-surface-1 lg:flex">
      <div className="flex h-14 items-center gap-2.5 px-4">
        <RunwayMark className="size-6" />
        <span className="text-[15px] font-semibold tracking-tight">Runway</span>
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 px-2 pt-2" aria-label="Primary">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              cn(
                'flex h-9 items-center gap-2.5 rounded-sm px-2.5 text-[13px] font-medium transition-colors duration-150',
                isActive
                  ? 'bg-surface-3 text-fg'
                  : 'text-fg-muted hover:bg-surface-2 hover:text-fg',
              )
            }
          >
            <item.icon className="size-4 shrink-0" aria-hidden />
            {item.label}
          </NavLink>
        ))}
      </nav>
      {version ? (
        <div className="flex items-center gap-1.5 px-4 py-3 text-[11px] text-fg-subtle">
          <span>v{version}</span>
          {updateAvailable ? (
            <TooltipProvider>
              <Tooltip content={`Runway v${data!.latest_version} is available`}>
                <a href={RELEASES_URL} target="_blank" rel="noreferrer">
                  <Badge variant="warning">update</Badge>
                </a>
              </Tooltip>
            </TooltipProvider>
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}
