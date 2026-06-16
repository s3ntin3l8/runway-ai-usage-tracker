import { NavLink } from 'react-router';
import { cn } from '@/lib/cn';
import { NAV_ITEMS } from './nav';

export function BottomNav() {
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
            <item.icon className="size-5" aria-hidden />
            {item.label}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
