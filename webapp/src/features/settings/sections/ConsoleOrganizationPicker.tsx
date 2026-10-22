import { useEffect, useRef, useState } from 'react';
import { discoverConsoleOrganizations } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { HelperText, Label } from '@/components/ui/Input';
import { errorMessage } from '@/lib/errorMessage';

function nextSelection(organizations: { id: string }[], selected: string): string {
  if (organizations.some((org) => org.id === selected)) return selected;
  if (organizations.length === 1) return organizations[0].id;
  return '';
}

export function ConsoleOrganizationPicker({ cookie, value, onSelect }: {
  cookie: string;
  value: string;
  onSelect: (id: string) => void;
}) {
  const [organizations, setOrganizations] = useState<{ id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectionRemoved, setSelectionRemoved] = useState(false);
  const inflight = useRef<AbortController | null>(null);
  const selection = useRef({ value, onSelect });
  useEffect(() => {
    selection.current = { value, onSelect };
  });
  const previousCookie = useRef(cookie);
  useEffect(() => {
    if (previousCookie.current !== cookie) {
      previousCookie.current = cookie;
      selection.current.onSelect('');
    }
    setOrganizations([]);
    setSelectionRemoved(false);
    setError(null);
    setLoading(false);
    return () => inflight.current?.abort();
  }, [cookie]);

  const load = async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    setLoading(true);
    setSelectionRemoved(false);
    setError(null);
    try {
      const data = await discoverConsoleOrganizations(cookie.trim(), ac.signal);
      if (ac.signal.aborted) return;
      setOrganizations(data.organizations);
      // Use the latest selection: it can change while discovery is in flight.
      const selected = selection.current.value;
      setSelectionRemoved(Boolean(selected) && !data.organizations.some((org) => org.id === selected));
      selection.current.onSelect(nextSelection(data.organizations, selected));
      if (data.organizations.length === 0) setError('No API organizations are accessible with this Console session.');
    } catch (err) {
      if (!ac.signal.aborted) setError(errorMessage(err, 'Could not load organizations'));
    } finally {
      if (!ac.signal.aborted) setLoading(false);
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <Button type="button" disabled={!cookie.trim() || loading} onClick={load}>
        {loading ? 'Loading organizations…' : 'Load Console organizations'}
      </Button>
      {organizations.length > 0 ? (
        <>
          <Label htmlFor="console-organization">API organization</Label>
          <select id="console-organization" value={value} onChange={(e) => {
            setSelectionRemoved(false);
            onSelect(e.target.value);
          }}
            className="rounded-md border border-edge bg-surface-1 p-2 text-sm text-fg">
            <option value="">Select an organization</option>
            {organizations.map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
          </select>
        </>
      ) : null}
      {selectionRemoved ? <HelperText>Previously selected organization is no longer accessible.</HelperText> : null}
      {error ? <HelperText error>{error}</HelperText> : null}
    </div>
  );
}
