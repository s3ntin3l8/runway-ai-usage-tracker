// Settings shell: desktop = two-pane (section nav + content), mobile =
// iOS-style list → subpage. Sections are nested routes for deep links.

import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router';
import {
  ArrowLeft,
  ChevronRight,
  FileClock,
  Info,
  KeyRound,
  Palette,
  Plug,
  Settings2,
  Webhook as WebhookIcon,
  type LucideIcon,
} from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { PageHeader } from '@/components/layout/PageHeader';
import { useIsDesktop } from '@/hooks/useMediaQuery';
import { cn } from '@/lib/cn';
import { AboutSection } from './sections/AboutSection';
import { AuditSection } from './sections/AuditSection';
import { DisplaySection } from './sections/DisplaySection';
import { ProvidersSection } from './sections/ProvidersSection';
import { SystemSection } from './sections/SystemSection';
import { TokensSection } from './sections/TokensSection';
import { WebhooksSection } from './sections/WebhooksSection';

interface Section {
  slug: string;
  label: string;
  description: string;
  icon: LucideIcon;
  element: React.ReactNode;
}

const SECTIONS: Section[] = [
  {
    slug: 'providers',
    label: 'Providers',
    description: 'API keys, cookies, collection strategies',
    icon: Plug,
    element: <ProvidersSection />,
  },
  {
    slug: 'tokens',
    label: 'Token health',
    description: 'Cached credentials and expiry',
    icon: KeyRound,
    element: <TokensSection />,
  },
  {
    slug: 'webhooks',
    label: 'Alerts',
    description: 'Discord / Slack threshold webhooks',
    icon: WebhookIcon,
    element: <WebhooksSection />,
  },
  {
    slug: 'system',
    label: 'System',
    description: 'Polling, timezone, maintenance',
    icon: Settings2,
    element: <SystemSection />,
  },
  {
    slug: 'display',
    label: 'Display',
    description: 'Theme',
    icon: Palette,
    element: <DisplaySection />,
  },
  {
    slug: 'audit',
    label: 'Audit log',
    description: 'Recorded admin mutations',
    icon: FileClock,
    element: <AuditSection />,
  },
  {
    slug: 'about',
    label: 'About',
    description: 'Version and collector status',
    icon: Info,
    element: <AboutSection />,
  },
];

export function SettingsPage() {
  const isDesktop = useIsDesktop();
  const location = useLocation();
  const navigate = useNavigate();
  const atIndex = /\/settings\/?$/.test(location.pathname);

  // On mobile subpages: show the section label as the title and a back button.
  const activeSection = !atIndex
    ? SECTIONS.find((s) => location.pathname.endsWith(`/${s.slug}`))
    : undefined;
  const showBack = !isDesktop && !atIndex;

  return (
    <>
      <PageHeader
        title={showBack && activeSection ? activeSection.label : 'Settings'}
        leading={
          showBack ? (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => navigate('/settings')}
              className="-ml-1"
              aria-label="Back to Settings"
            >
              <ArrowLeft className="size-4" aria-hidden />
            </Button>
          ) : undefined
        }
      />
      <div className="lg:flex">
        {/* Section nav: persistent pane on desktop; index list on mobile */}
        {(isDesktop || atIndex) && (
          <nav
            aria-label="Settings sections"
            className="flex flex-col gap-0.5 p-3 lg:w-60 lg:shrink-0 lg:border-r lg:border-edge lg:p-4"
          >
            {SECTIONS.map((s) => (
              <NavLink
                key={s.slug}
                to={`/settings/${s.slug}`}
                className={({ isActive }) =>
                  cn(
                    'flex min-h-11 items-center gap-3 rounded-sm px-2.5 py-2 transition-colors duration-150',
                    isActive && isDesktop
                      ? 'bg-surface-3 text-fg'
                      : 'text-fg-muted hover:bg-surface-2 hover:text-fg',
                  )
                }
              >
                <s.icon className="size-4 shrink-0" aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium text-fg">{s.label}</span>
                  <span className="block truncate text-[11px] text-fg-subtle lg:hidden">
                    {s.description}
                  </span>
                </span>
                <ChevronRight className="size-4 text-fg-subtle lg:hidden" aria-hidden />
              </NavLink>
            ))}
          </nav>
        )}

        <div className="min-w-0 flex-1">
          <Routes>
            <Route
              index
              element={isDesktop ? <Navigate to="/settings/providers" replace /> : <span />}
            />
            {SECTIONS.map((s) => (
              <Route
                key={s.slug}
                path={s.slug}
                element={<div className="p-4 lg:p-8">{s.element}</div>}
              />
            ))}
            <Route path="*" element={<Navigate to="/settings" replace />} />
          </Routes>
        </div>
      </div>
    </>
  );
}
