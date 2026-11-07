// One line per account for the Claude cloud-runner collector: its last cycle's
// outcome and how long ago it ran. Warning style when the server calls it unhealthy.
import { useQuery } from '@tanstack/react-query';
import { fetchStatus } from '@/api/endpoints';
import { maskAccountId } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';

const OUTCOME_LABELS: Record<string, string> = {
  ok: 'OK',
  partial: 'catching up',
  truncated: 'truncated',
  busy: 'busy',
  migration_required: 'migration required',
  storage_error: 'storage error',
  internal_error: 'internal error',
  source_ineligible: 'login not eligible',
  never_run: 'not run yet',
};

export function ClaudeCloudHealthLines() {
  const status = useQuery({
    queryKey: ['system', 'status'],
    queryFn: fetchStatus,
    refetchInterval: 60_000,
  });
  const accounts = status.data?.claude_cloud?.accounts ?? [];
  if (accounts.length === 0) return null;

  return (
    <div className="flex flex-col gap-1 rounded-md border border-edge bg-surface-1 p-3" data-testid="claude-cloud-health">
      <p className="text-[11px] font-medium uppercase tracking-wide text-fg-muted">Cloud-runner collector</p>
      {accounts.map((a) => {
        const outcome = OUTCOME_LABELS[a.last_outcome] ?? a.last_outcome;
        const ago = a.last_cycle_at > 0 ? timeAgo(new Date(a.last_cycle_at * 1000).toISOString()) : 'never';
        return (
          <p
            key={a.account_id}
            data-healthy={a.healthy}
            className={`text-[12px] ${a.healthy ? 'text-fg-muted' : 'text-warning'}`}
          >
            <code className="font-mono text-[11px]">{maskAccountId(a.account_id)}</code>
            {' · '}
            {outcome}
            {' · '}
            {ago}
            {a.healthy ? '' : ' · needs attention'}
          </p>
        );
      })}
    </div>
  );
}
