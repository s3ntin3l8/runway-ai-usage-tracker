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
