// Project labels: extra worktree directory names, added to the built-in ones
// (.wt, .worktrees, ...). The server cuts a session's cwd at the first
// matching directory so every worktree of a repository shares its label.

import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { putAppConfig } from '@/api/endpoints';
import type { AppConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { HelperText, Input, Label } from '@/components/ui/Input';
import { patternError } from '@/lib/worktreePatterns';

export function WorktreePatternsCard({ config }: { config: AppConfig | undefined }) {
  const queryClient = useQueryClient();
  const saved = config?.worktree_patterns;
  const builtin = config?.worktree_builtin_patterns ?? [];
  const [patterns, setPatterns] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPatterns(saved ?? []);
  }, [saved]);

  const save = useMutation({
    mutationFn: () => putAppConfig({ worktree_patterns: patterns }),
    onSuccess: () => {
      toast.success('Worktree patterns saved');
      queryClient.invalidateQueries({ queryKey: ['system', 'app-config'] });
    },
    onError: (err) => toast.error(err.message),
  });

  const add = () => {
    const problem = patternError(draft, patterns);
    setError(problem);
    if (problem) return;
    setPatterns([...patterns, draft.trim()]);
    setDraft('');
  };

  const dirty = JSON.stringify(patterns) !== JSON.stringify(saved ?? []);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Project labels</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="sys-wt-pattern">Extra worktree directories</Label>
          <div className="flex gap-2">
            <Input
              id="sys-wt-pattern"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  add();
                }
              }}
              placeholder="e.g. *-wt"
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? 'sys-wt-pattern-error' : undefined}
            />
            <Button type="button" onClick={add}>
              Add
            </Button>
          </div>
          {error ? (
            <p id="sys-wt-pattern-error" role="alert" className="text-xs text-critical">
              {error}
            </p>
          ) : null}
          <HelperText>
            Sessions run in a worktree get the repository's label instead of their own. A name
            such as <code>worktrees-extra</code> marks a container: the path is cut before it. A
            pattern starting with <code>*</code> such as <code>*-wt</code> marks a folder kept
            beside the repository: <code>repo-wt/slug</code> is labelled <code>repo</code>.
            Matching ignores case. Built-in: {builtin.length ? builtin.join(', ') : '.wt, .claude, ...'}.
            After saving, apply the "worktree project labels" check in Data Health to relabel
            history.
          </HelperText>
        </div>
        {patterns.length ? (
          <ul className="flex flex-wrap gap-2" aria-label="Worktree patterns">
            {patterns.map((p) => (
              <li key={p}>
                <Badge className="gap-1.5">
                  <code>{p}</code>
                  <button
                    type="button"
                    aria-label={`Remove ${p}`}
                    className="text-fg-muted hover:text-fg"
                    onClick={() => setPatterns(patterns.filter((x) => x !== p))}
                  >
                    ×
                  </button>
                </Badge>
              </li>
            ))}
          </ul>
        ) : null}
        <Button
          type="button"
          variant="primary"
          className="self-start"
          disabled={!dirty}
          loading={save.isPending}
          onClick={() => save.mutate()}
        >
          Save patterns
        </Button>
      </CardContent>
    </Card>
  );
}
