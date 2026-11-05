import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { deleteSidecar } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';

export function DeleteSidecarDialog({
  sidecar,
  onClose,
}: {
  sidecar: Sidecar | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const del = useMutation({
    mutationFn: (id: string) => deleteSidecar(id),
    onSuccess: () => {
      toast.success('Sidecar removed');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
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
      title="Remove sidecar?"
      description={sidecar?.custom_name || sidecar?.hostname || sidecar?.sidecar_id}
    >
      <p className="text-sm text-fg-muted">
        The registry entry is removed; collected usage events stay. The sidecar re-registers if it
        keeps running and checks in again.
      </p>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="danger"
          onClick={() => sidecar && del.mutate(sidecar.sidecar_id)}
          loading={del.isPending}
        >
          Remove
        </Button>
      </div>
    </ResponsiveDialog>
  );
}
