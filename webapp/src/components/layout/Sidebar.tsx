import { NavLink } from 'react-router';
import { cn } from '@/lib/cn';
import { NAV_ITEMS } from './nav';
import { RunwayMark } from './RunwayMark';

export function Sidebar() {
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
    </aside>
  );
}
