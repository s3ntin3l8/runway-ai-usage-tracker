// Machine → the credentials it reports. Replaces Fleet's "current account identities" panel.

import { Link } from 'react-router';
import type { CredentialInventory, CredentialSourceView } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { displayAccountName } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';
import { SourceList } from './SourceList';

export function ByMachineView({ inventory }: { inventory: CredentialInventory }) {
  // Index sources by machine, remembering their provider/account for the row context.
  const byMachine = new Map<string, { source: CredentialSourceView; context: string }[]>();
  for (const provider of inventory.providers) {
    for (const account of provider.accounts) {
      for (const source of account.sources) {
        if (!source.machine_id) continue;
        const who = account.identity_pending ? 'needs an account' : displayAccountName(account);
        const rows = byMachine.get(source.machine_id) ?? [];
        rows.push({ source, context: `${provider.name} · ${who}` });
        byMachine.set(source.machine_id, rows);
      }
    }
  }

  const contexts = new Map<CredentialSourceView, string>();
  for (const rows of byMachine.values()) for (const r of rows) contexts.set(r.source, r.context);

  return (
    <div className="space-y-3">
      {inventory.machines.map((m) => {
        const rows = byMachine.get(m.machine_id) ?? [];
        return (
          <Card key={m.machine_id} className="p-4" id={`machine-${m.machine_id}`}>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold">{m.name}</h3>
              <span className="text-[11px] text-fg-subtle">last seen {timeAgo(m.last_seen)}</span>
              {m.stale ? <Badge variant="warning">offline</Badge> : null}
              {m.unmapped_count > 0 ? (
                <Link to="?view=mapping" aria-label={`${m.name}: ${m.unmapped_count} need mapping`}>
                  <Badge variant="warning">{m.unmapped_count} need mapping</Badge>
                </Link>
              ) : null}
            </div>
            {m.login_dirs_report ? (
              <details className="mt-2 text-[12px] text-fg-muted">
                <summary className="cursor-pointer">Login directories searched</summary>
                <p className="mt-1">Directory candidates from the last check-in, including missing locations.</p>
                {Object.entries(m.login_dirs_report.searched).map(([provider, paths]) => (
                  <div key={provider} className="mt-2">
                    <span className="font-medium">{provider === 'anthropic' ? 'Claude Code' : 'Codex'}</span>
                    <ul className="mt-1 space-y-1">{paths?.map(path => <li key={path} className="break-all font-mono">{path}</li>)}</ul>
                  </div>
                ))}
                <Link className="mt-2 inline-block text-accent-text underline" to={`/fleet?settings=${encodeURIComponent(m.machine_id)}`}>Configure login directories</Link>
              </details>
            ) : (
              <p className="mt-2 text-[12px] text-fg-subtle">Login directories are not reported by this sidecar yet.</p>
            )}
            {rows.length === 0 ? (
              <p className="mt-2 text-[12px] text-fg-muted">This machine reports no credentials.</p>
            ) : (
              <SourceList
                sources={rows.map((r) => r.source)}
                label={`${m.name} credentials`}
                contextFor={(s) => contexts.get(s) ?? ''}
                showMachine={false}
              />
            )}
          </Card>
        );
      })}
    </div>
  );
}
