import type { UnmergePlan } from '@/api/types';

/**
 * The tables a merge moves, in words the operator can read.
 */
const UNMERGE_TABLE_LABELS: Record<string, string> = {
  usage_events: 'usage events',
  latest_usage: 'quota cards',
  latest_usage_contributions: 'quota card sources',
  usage_windows: 'usage windows',
  quota_snapshots: 'quota snapshots',
};

export function unmergeSummary(plan: UnmergePlan): string {
  const parts = Object.entries(plan.rows).map(
    ([table, count]) => `${count} ${UNMERGE_TABLE_LABELS[table] ?? table.replaceAll('_', ' ')}`,
  );
  if (plan.credential_sources.length > 0) {
    parts.push(
      `${plan.credential_sources.length} credential source${plan.credential_sources.length === 1 ? '' : 's'}`,
    );
  }
  if (plan.contributions > 0) {
    parts.push(`${plan.contributions} credential bundle${plan.contributions === 1 ? '' : 's'}`);
  }
  if (plan.credential_tags > 0) {
    parts.push(`${plan.credential_tags} tag${plan.credential_tags === 1 ? '' : 's'}`);
  }
  return parts.length > 0 ? `Back to this account: ${parts.join(', ')}.` : 'Nothing to move back.';
}
