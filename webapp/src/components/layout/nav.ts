import { Gauge, History, Server, Settings, Sparkles, type LucideIcon } from 'lucide-react';

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /**
   * Match this path exactly. Without it the item stays active for its
   * descendants (`/settings/providers` keeps `/settings` active).
   */
  end?: boolean;
  /**
   * Additional paths that keep this item active. The provider detail route
   * is a drill-down from Home, so Home stays the active item there.
   */
  alsoActive?: string[];
}

// Shared by the desktop sidebar and the mobile bottom nav (≤5 items).
export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Home', icon: Gauge, end: true, alsoActive: ['/provider'] },
  { to: '/insights', label: 'Insights', icon: Sparkles },
  { to: '/history', label: 'History', icon: History },
  { to: '/fleet', label: 'Fleet', icon: Server },
  { to: '/settings', label: 'Settings', icon: Settings },
];

/**
 * Whether a nav item is the current location.
 *
 * The provider detail route (/provider/:id) is a drill-down from Home, so
 * Home has to stay the active item there — otherwise the second-most-visited
 * screen shows no highlighted location at all, in either nav. NavLink cannot
 * express "exactly / or anything under /provider/*" (it computes a single
 * match and always owns aria-current), so the navs render plain links and
 * use this matcher for both the active class and aria-current.
 */
export function isNavItemActive(item: NavItem, pathname: string): boolean {
  if (pathname === item.to) return true;
  // `end` means this path and nothing below it. It does not cancel an
  // explicit alsoActive path: Home owns the provider drill-down.
  if (!item.end && pathname.startsWith(`${item.to}/`)) return true;
  return (item.alsoActive ?? []).some(
    (path) => pathname === path || pathname.startsWith(`${path}/`),
  );
}
