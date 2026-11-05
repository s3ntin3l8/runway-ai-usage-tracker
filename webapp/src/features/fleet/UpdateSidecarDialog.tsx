import { useMutation } from '@tanstack/react-query';
import { toast } from 'sonner';
import { triggerSidecarUpdate } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';

export function UpdateSidecarDialog({
  sidecar,
  onClose,
}: {
  sidecar: Sidecar | null;
  onClose: () => void;
}) {
  const update = useMutation({
    mutationFn: (id: string) => triggerSidecarUpdate(id),
    onSuccess: () => {
      toast.success('Update pushed — the sidecar installs it on its next check-in');
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <ResponsiveDialog
      open={sidecar !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Push update?"
      description={sidecar?.custom_name || sidecar?.hostname || sidecar?.sidecar_id}
    >
      <p className="text-sm text-fg-muted">
        Queues the latest build for this sidecar. It downloads, verifies, and installs the
        update on its next check-in, then restarts itself. Collection resumes automatically.
      </p>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="primary"
          onClick={() => sidecar && update.mutate(sidecar.sidecar_id)}
          loading={update.isPending}
        >
          Update
        </Button>
      </div>
    </ResponsiveDialog>
  );
}
