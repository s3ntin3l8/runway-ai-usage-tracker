import { Gauge, History, Server, Settings, Sparkles, type LucideIcon } from 'lucide-react';

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
}

// Shared by the desktop sidebar and the mobile bottom nav (≤5 items).
export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Home', icon: Gauge, end: true },
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
  if (item.to === '/') return pathname === '/' || pathname.startsWith('/provider/');
  return pathname === item.to || pathname.startsWith(`${item.to}/`);
}
