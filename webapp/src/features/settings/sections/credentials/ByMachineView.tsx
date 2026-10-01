// Machine → the credentials it reports. Replaces Fleet's "current account identities" panel.

import { Link } from 'react-router';
import type { CredentialInventory, CredentialSourceView } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { displayAccountName } from '@/lib/accountDisplay';
import { timeAgo } from '@/lib/format';
import { SourceRow } from './SourceRow';

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

  return (
    <div className="space-y-3">
      {inventory.machines.map((m) => {
        const rows = byMachine.get(m.machine_id) ?? [];
        return (
          <Card key={m.machine_id} className="p-4" id={`machine-${m.machine_id}`}>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold">{m.name}</h3>
              <span className="text-[11px] text-fg-subtle">last seen {timeAgo(m.last_seen)}</span>
              {m.unmapped_count > 0 ? (
                <Link to="?view=mapping" aria-label={`${m.name}: ${m.unmapped_count} need mapping`}>
                  <Badge variant="warning">{m.unmapped_count} need mapping</Badge>
                </Link>
              ) : null}
            </div>
            {rows.length === 0 ? (
              <p className="mt-2 text-[12px] text-fg-muted">This machine reports no credentials.</p>
            ) : (
              <ul className="mt-1 divide-y divide-edge" aria-label={`${m.name} credentials`}>
                {rows.map(({ source, context }) => (
                  <SourceRow
                    key={`${source.provider_id}/${source.account_id}/${source.source_id}`}
                    source={source}
                    context={context}
                  />
                ))}
              </ul>
            )}
          </Card>
        );
      })}
    </div>
  );
}
