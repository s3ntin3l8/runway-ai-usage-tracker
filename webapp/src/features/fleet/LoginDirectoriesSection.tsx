import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { setSidecarSettings } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Label } from '@/components/ui/Input';
import { loginDirsPending } from './settingsSync';

export function LoginDirectoriesSection({ sidecar, online, onUpdate }: {
  sidecar: Sidecar; online: boolean; onUpdate: () => void;
}) {
  const queryClient = useQueryClient();
  const desired = sidecar.login_dirs_desired ?? { claude_config_dirs: [], codex_home: [] };
  const [claude, setClaude] = useState(desired.claude_config_dirs.join('\n'));
  const [codex, setCodex] = useState(desired.codex_home.join('\n'));
  const split = (text: string) => [...new Set(text.split('\n').map(p => p.trim()).filter(Boolean))];
  const save = useMutation({
    mutationFn: (clear: boolean) => setSidecarSettings(sidecar.sidecar_id, {
      claude_config_dirs: clear ? null : split(claude),
      codex_home: clear ? null : split(codex),
    }),
    onSuccess: (_data, clear) => {
      if (clear) { setClaude(''); setCodex(''); }
      toast.success('Login directories saved — apply on next check-in');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: err => toast.error(err.message),
  });
  const report = sidecar.login_dirs_report;
  const pending = loginDirsPending(sidecar);
  return (
    <section className="rounded-lg border border-edge p-4">
      <h3 className="text-[13px] font-semibold">Login directories</h3>
      <p className="mt-1 text-[11px] text-fg-subtle">
        Extra Claude Code and Codex directories on this machine, one per line. Standard locations,
        environment variables and the sidecar's local configuration are also searched. Use directories,
        not credential files; ~ refers to the user running the sidecar.
      </p>
      {!report ? (
        <p className="mt-2 text-[11px] text-fg-muted">
          This sidecar does not report login directories yet. Update it to a newer build to configure them.
          {sidecar.update_available ? <button type="button" className="ml-1 text-accent-text underline" onClick={onUpdate}>Update it</button> : null}
        </p>
      ) : (
        <form className="mt-3 flex flex-col gap-2" onSubmit={e => { e.preventDefault(); save.mutate(false); }}>
          {([['claude', 'Claude Code directories', claude, setClaude], ['codex', 'Codex directories', codex, setCodex]] as const).map(([key, label, value, update]) => (
            // Most devices add none, so the field stays folded until it has content or is opened.
            <details key={key} open={value.trim() !== '' || undefined} className="text-xs">
              <summary className="cursor-pointer text-accent-text">
                {label} · {split(value).length ? `${split(value).length} added` : 'none added'}
              </summary>
              <div className="mt-1.5 flex flex-col gap-1">
                <Label htmlFor={`${sidecar.sidecar_id}-${key}-dirs`}>{label}</Label>
                <textarea id={`${sidecar.sidecar_id}-${key}-dirs`} rows={3} value={value} onChange={e => update(e.target.value)} disabled={save.isPending}
                  className="w-full rounded-sm border border-edge bg-surface-1 p-2 text-sm text-fg focus:border-accent" />
              </div>
            </details>
          ))}
          <p className="text-[11px] text-fg-muted" role="status">
            {!online ? 'Sidecar is offline — applies when it reconnects.' : pending ? 'Requested directories — waiting for the sidecar to confirm.' : 'Fleet directories confirmed by the sidecar.'}
          </p>
          <div className="flex gap-2">
            <Button size="sm" type="submit" loading={save.isPending}>Save directories</Button>
            <Button size="sm" type="button" disabled={save.isPending} onClick={() => save.mutate(true)}>Clear Fleet additions</Button>
          </div>
        </form>
      )}
    </section>
  );
}
