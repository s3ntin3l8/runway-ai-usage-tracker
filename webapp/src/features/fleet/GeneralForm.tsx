import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { patchSidecar } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Input, Label } from '@/components/ui/Input';

export function GeneralForm({ sidecar }: { sidecar: Sidecar }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(sidecar.custom_name ?? '');
  const [tags, setTags] = useState((sidecar.tags ?? []).join(', '));
  const parseTags = (text: string) =>
    text
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);

  const save = useMutation({
    mutationFn: () => patchSidecar(sidecar.sidecar_id, { custom_name: name.trim(), tags: parseTags(tags) }),
    onSuccess: () => {
      toast.success('Sidecar updated');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });
  // Saved on blur (or Enter) when something actually changed; there is no Save button.
  const commit = () => {
    const unchanged =
      name.trim() === (sidecar.custom_name ?? '') &&
      JSON.stringify(parseTags(tags)) === JSON.stringify(sidecar.tags ?? []);
    if (!unchanged && !save.isPending) save.mutate();
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        commit();
      }}
      className="flex flex-col gap-3 rounded-lg border border-edge p-4"
    >
      <h3 className="text-[13px] font-semibold">Identity</h3>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-name">Display name</Label>
        <Input
          id="sidecar-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commit}
          placeholder={sidecar.hostname}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-tags">Tags (comma-separated)</Label>
        <Input
          id="sidecar-tags"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          onBlur={commit}
          placeholder="work, laptop"
        />
      </div>
      <p className="text-[11px] text-fg-subtle">Saved when you leave a field.</p>
    </form>
  );
}
