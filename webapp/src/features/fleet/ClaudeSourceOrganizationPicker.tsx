import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { fetchClaudeSourceOrganizations, selectClaudeSourceOrganization } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { Label } from '@/components/ui/Input';

interface Props { sourceId: string; providerId: string; onComplete: () => void }

// A keyed child keeps both pending mutations and selections attached to their source.
export function ClaudeSourceOrganizationPicker(props: Props) {
  return <SourcePicker key={`${props.providerId}/${props.sourceId}`} {...props} />;
}
function SourcePicker({ sourceId, providerId, onComplete }: Props) {
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [selected, setSelected] = useState('');
  const query = useQuery({
    queryKey: ['fleet', 'claude-organizations', providerId, sourceId],
    queryFn: () => fetchClaudeSourceOrganizations(providerId, sourceId),
    retry: false,
    staleTime: 0,
  });
  const save = useMutation({
    mutationFn: (id: string) => selectClaudeSourceOrganization(providerId, sourceId, id),
    onSuccess: () => { if (mounted.current) onComplete(); },
  });
  if (query.isPending) return <p role="status" className="mt-2 text-sm text-fg-muted">Verifying Claude organizations…</p>;
  if (query.isError) return <div className="mt-2"><p role="alert" className="text-sm text-warning">Could not verify this Claude source. Try again after the next sidecar check.</p><Button onClick={() => query.refetch()}>Retry verification</Button></div>;
  const data = query.data;
  if (data.complete && data.organizations.length === 0) return <div className="mt-2 space-y-2">
    <p role="alert" className="text-sm text-warning">No eligible Claude organization is available for this source. Check that this login has access to the requested Claude service, then retry verification.</p>
    <Button disabled={query.isFetching} onClick={() => query.refetch()}>Retry verification</Button>
  </div>;
  if (data.complete && data.organizations.length === 1 && !data.selection_required) return <p role="status" className="mt-2 text-sm text-fg-muted">Organization verified. Waiting for the sidecar to finish assigning this source.</p>;
  const eligible = data.organizations.some((org) => org.id === selected);
  const id = `claude-org-${providerId}-${sourceId}`;
  return <div className="mt-3 space-y-2">
    {!data.complete && <p role="status" className="text-sm text-fg-muted">Organization verification is incomplete. Wait for a successful sidecar check before confirming.</p>}
    <Label htmlFor={id}>Claude organization</Label>
    <select id={id} value={selected} onChange={(event) => setSelected(event.target.value)} disabled={!data.complete || save.isPending} className="w-full rounded-md border border-border bg-surface-1 px-3 py-2 text-sm text-fg">
      <option value="">Pick an organization…</option>
      {data.organizations.map((org) => <option key={org.id} value={org.id}>{org.name || org.id}</option>)}
    </select>
    {save.isError && <p role="alert" className="text-sm text-warning">Could not assign this organization. Verify the source again before retrying.</p>}
    <Button variant="primary" disabled={!data.complete || !eligible || query.isFetching || save.isError} loading={save.isPending} onClick={() => save.mutate(selected)}>Confirm organization</Button>
    {(!data.complete || save.isError) && <Button onClick={() => { save.reset(); setSelected(''); void query.refetch(); }}>Retry verification</Button>}
  </div>;
}
