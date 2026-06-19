import { NavLink } from 'react-router';
import { cn } from '@/lib/cn';
import { useFleetUpdateCount } from '@/features/fleet/queries';
import { NAV_ITEMS } from './nav';

export function BottomNav() {
  // Reuses the shared ['fleet','sidecars'] cache — no extra request.
  const fleetUpdates = useFleetUpdateCount();
  return (
    <nav
      aria-label="Primary"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-edge bg-surface-1/95 backdrop-blur-sm pb-[env(safe-area-inset-bottom)] lg:hidden"
    >
      <div className="grid h-16 grid-cols-5">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              cn(
                'flex min-h-11 flex-col items-center justify-center gap-1 text-[11px] font-medium transition-colors duration-150',
                isActive ? 'text-accent' : 'text-fg-muted active:text-fg',
              )
            }
          >
            <span className="relative">
              <item.icon className="size-5" aria-hidden />
              {item.to === '/fleet' && fleetUpdates > 0 ? (
                <>
                  <span
                    className="absolute -right-1.5 -top-1 size-2 rounded-full bg-warning ring-2 ring-surface-1"
                    aria-hidden
                  />
                  <span className="sr-only">{fleetUpdates} sidecar updates available</span>
                </>
              ) : null}
            </span>
            {item.label}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
