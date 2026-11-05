import { timeUntil } from '@/lib/format';

export function nextPollLabel(iso: string | null | undefined): string {
  const until = timeUntil(iso);
  if (!until) return '—';
  return until === 'now' ? 'now' : `in ${until}`;
}
