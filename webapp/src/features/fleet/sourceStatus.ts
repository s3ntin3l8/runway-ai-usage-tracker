// Labels, badge variants, and dots for the five device-permission states a source row shows.

import type { BadgeProps } from '@/components/ui/Badge';
import type { MacStatus } from './sourceModel';

export const STATUS: Record<MacStatus, { label: string; variant: BadgeProps['variant']; dot: string }> = {
  ok: { label: 'Authorized', variant: 'ok', dot: 'bg-ok' },
  need: { label: 'Setup on Mac', variant: 'warning', dot: 'bg-warning' },
  busy: { label: 'Connecting…', variant: 'unlimited', dot: 'bg-unlimited' },
  err: { label: 'Action required', variant: 'critical', dot: 'bg-critical' },
  off: { label: 'Off', variant: 'outline', dot: 'border border-fg-subtle' },
};
