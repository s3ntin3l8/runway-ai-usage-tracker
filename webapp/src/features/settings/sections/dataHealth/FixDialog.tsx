// Preview → confirm → apply → job-progress flow for one fixable finding
// group. Apply only unlocks once a preview has run with the *exact* params
// about to be applied — editing a param after previewing re-locks it, so
// the operator never applies a fix they haven't seen a summary for.

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { toast } from 'sonner';
import type { DataHealthFindingGroup } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import { Switch } from '@/components/ui/Switch';
import {
  dataHealthKey,
  useApplyDataHealthFix,
  useDataHealthJob,
  usePreviewDataHealthFix,
} from './queries';

interface FixDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  checkId: string;
  group: DataHealthFindingGroup;
}

// Only non-empty values go to the server — an empty text param means "let
// the server infer it," matching how each check's plan()/apply() already
// treat a missing key.
function cleanParams(values: Record<string, string>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ''));
}

function sameParams(a: Record<string, string>, b: Record<string, string>): boolean {
  const cleanA = cleanParams(a);
  const cleanB = cleanParams(b);
  const keys = new Set([...Object.keys(cleanA), ...Object.keys(cleanB)]);
  return [...keys].every((k) => cleanA[k] === cleanB[k]);
}

export function FixDialog({ open, onOpenChange, checkId, group }: FixDialogProps) {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [previewedValues, setPreviewedValues] = useState<Record<string, string> | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);

  const preview = usePreviewDataHealthFix();
  const apply = useApplyDataHealthFix();
  const job = useDataHealthJob(jobId);

  // Reset per-open, so a dialog reused for a different group never carries
  // over another group's stale preview/confirmation state.
  useEffect(() => {
    if (open) {
      setValues({});
      setPreviewedValues(null);
      setConfirmed(false);
      setJobId(null);
      preview.reset();
      apply.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, group.key]);

  useEffect(() => {
    if (job.data?.status === 'succeeded') {
      toast.success(job.data.result?.summary ?? 'Fix applied');
      queryClient.invalidateQueries({ queryKey: dataHealthKey });
    } else if (job.data?.status === 'failed') {
      toast.error(job.data.error ?? 'Fix failed');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.data?.status]);

  const canApply =
    previewedValues !== null &&
    sameParams(previewedValues, values) &&
    confirmed &&
    jobId === null;

  const runPreview = () => {
    const snapshot = { ...values };
    preview.mutate(
      { checkId, groupKey: group.key, params: cleanParams(values) },
      { onSuccess: () => setPreviewedValues(snapshot) },
    );
  };

  const runApply = () => {
    apply.mutate(
      { checkId, groupKey: group.key, params: cleanParams(values) },
      {
        onSuccess: (result) => setJobId(result.job_id),
        onError: (err) => toast.error(err.message),
      },
    );
  };

  const jobDone = job.data?.status === 'succeeded' || job.data?.status === 'failed';

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={(next) => {
        if (!next && job.data?.status === 'running') return; // don't let a running job vanish
        onOpenChange(next);
      }}
      title={`Fix: ${group.label}`}
      description={
        group.not_fixable_reason ? undefined : 'Preview the change, then confirm to apply it.'
      }
    >
      <div className="flex flex-col gap-4">
        {group.params.length > 0 && (
          <div className="flex flex-col gap-3">
            {group.params.map((param) => (
              <div key={param.name} className="flex flex-col gap-1.5">
                <Label htmlFor={`dh-param-${param.name}`}>
                  {param.label}
                  {param.required ? '' : ' (optional)'}
                </Label>
                {param.options && param.options.length > 0 ? (
                  <Select
                    value={values[param.name] ?? ''}
                    onValueChange={(v) => {
                      setValues((prev) => ({ ...prev, [param.name]: v }));
                      setPreviewedValues(null);
                    }}
                  >
                    <SelectTrigger id={`dh-param-${param.name}`}>
                      <SelectValue placeholder="Select…" />
                    </SelectTrigger>
                    <SelectContent>
                      {param.options.map((opt) => (
                        <SelectItem key={opt} value={opt}>
                          {opt}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <Input
                    id={`dh-param-${param.name}`}
                    value={values[param.name] ?? ''}
                    onChange={(e) => {
                      const v = e.target.value;
                      setValues((prev) => ({ ...prev, [param.name]: v }));
                      setPreviewedValues(null);
                    }}
                  />
                )}
              </div>
            ))}
          </div>
        )}

        {jobId === null ? (
          <>
            <Button
              variant="secondary"
              onClick={runPreview}
              loading={preview.isPending}
              disabled={preview.isPending}
            >
              Preview
            </Button>

            {preview.isError && (
              <p className="text-[12px] text-critical">{preview.error.message}</p>
            )}

            {preview.data && (
              <div className="flex flex-col gap-2 rounded-md border border-edge bg-surface-1 p-3">
                <p className="text-[13px] font-medium text-fg">{preview.data.summary}</p>
                <dl className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-fg-subtle">
                  {Object.entries(preview.data.counts).map(([key, value]) => (
                    <div key={key} className="flex gap-1">
                      <dt className="font-medium">{key}:</dt>
                      <dd className="font-mono">{String(value)}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}

            {preview.data && (
              <div className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
                <Label htmlFor="dh-confirm">I've reviewed the preview — apply this fix</Label>
                <Switch id="dh-confirm" checked={confirmed} onCheckedChange={setConfirmed} />
              </div>
            )}

            <Button
              variant="primary"
              onClick={runApply}
              disabled={!canApply}
              loading={apply.isPending}
            >
              Apply fix
            </Button>
          </>
        ) : (
          <div className="flex flex-col gap-2 rounded-md border border-edge bg-surface-1 p-3">
            <div className="flex items-center gap-2 text-[13px] font-medium">
              {job.data?.status === 'running' && (
                <>
                  <Loader2 className="size-4 animate-spin text-fg-muted" aria-hidden />
                  Applying…
                </>
              )}
              {job.data?.status === 'succeeded' && (
                <>
                  <CheckCircle2 className="size-4 text-ok" aria-hidden />
                  {job.data.result?.summary ?? 'Fix applied'}
                </>
              )}
              {job.data?.status === 'failed' && (
                <>
                  <XCircle className="size-4 text-critical" aria-hidden />
                  Fix failed
                </>
              )}
            </div>
            {job.data?.status === 'failed' && job.data.error && (
              <p className="text-[12px] text-critical">{job.data.error}</p>
            )}
            {job.data?.status === 'succeeded' && job.data.result && (
              <dl className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-fg-subtle">
                {Object.entries(job.data.result.counts).map(([key, value]) => (
                  <div key={key} className="flex gap-1">
                    <dt className="font-medium">{key}:</dt>
                    <dd className="font-mono">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            )}
            {jobDone && (
              <Button size="sm" variant="secondary" onClick={() => onOpenChange(false)}>
                Close
              </Button>
            )}
          </div>
        )}
      </div>
    </ResponsiveDialog>
  );
}
