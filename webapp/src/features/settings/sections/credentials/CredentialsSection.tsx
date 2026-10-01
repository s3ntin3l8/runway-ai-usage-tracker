// Credentials: every credential Runway found, which account it maps to, and where each
// account's data actually comes from — in one place. Replaces Token health and absorbs
// Fleet's assignment rules / account identities and the credential list in Providers.

import { useSearchParams } from 'react-router';
import { KeyRound } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { ByMachineView } from './ByMachineView';
import { ByProviderView } from './ByProviderView';
import { NeedsMappingView } from './NeedsMappingView';
import { RulesView } from './RulesView';
import { useCredentialInventory } from './queries';

const VIEWS = ['provider', 'machine', 'mapping', 'rules'] as const;
type View = (typeof VIEWS)[number];

export function CredentialsSection() {
  const [params, setParams] = useSearchParams();
  const requested = params.get('view');
  const view: View = (VIEWS as readonly string[]).includes(requested ?? '')
    ? (requested as View)
    : 'provider';
  const inventory = useCredentialInventory();

  const setView = (next: string) => {
    const copy = new URLSearchParams(params);
    copy.set('view', next);
    setParams(copy, { replace: true });
  };

  if (inventory.isPending) {
    return <Skeleton className="h-48 w-full" />;
  }
  if (inventory.isError) {
    return (
      <p className="text-[13px] text-critical">
        Couldn't load credentials: {inventory.error.message}
      </p>
    );
  }

  const data = inventory.data;
  const needsMapping = data.unmapped_count + data.pending_usage_events;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">Credentials</h2>
        <p className="text-[12px] text-fg-muted">
          Which credentials were found, on which machine, which account each belongs to, and
          where each account's data is coming from.
        </p>
      </div>
      <Tabs value={view} onValueChange={setView}>
        <TabsList>
          <TabsTrigger value="provider">By provider</TabsTrigger>
          <TabsTrigger value="machine">By machine</TabsTrigger>
          <TabsTrigger value="mapping">
            Needs mapping
            {needsMapping > 0 ? <Badge variant="warning">{needsMapping}</Badge> : null}
          </TabsTrigger>
          <TabsTrigger value="rules">
            Rules
            {data.rule_count > 0 ? <Badge variant="neutral">{data.rule_count}</Badge> : null}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="provider">
          {data.providers.length === 0 ? (
            <EmptyState
              icon={KeyRound}
              title="No credentials found yet"
              description="Credentials appear once a sidecar reports them, a key is saved under Providers, or the server finds one in its environment."
            />
          ) : (
            <ByProviderView providers={data.providers} />
          )}
        </TabsContent>
        <TabsContent value="machine">
          {data.machines.length === 0 ? (
            <EmptyState
              icon={KeyRound}
              title="No machines yet"
              description="Pair a sidecar from the Fleet page to see what it reports."
            />
          ) : (
            <ByMachineView inventory={data} />
          )}
        </TabsContent>
        <TabsContent value="mapping">
          <NeedsMappingView pendingUsageEvents={data.pending_usage_events} />
        </TabsContent>
        <TabsContent value="rules">
          <RulesView />
        </TabsContent>
      </Tabs>
    </div>
  );
}
