// A small confirm step for destructive actions (remove a credential, delete a rule).

import { Button } from '@/components/ui/Button';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  confirmLabel?: string;
  pending?: boolean;
  onConfirm: () => void;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = 'Remove',
  pending = false,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <ResponsiveDialog open={open} onOpenChange={onOpenChange} title={title} description={description}>
      <div className="mt-4 flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)} disabled={pending}>
          Cancel
        </Button>
        <Button variant="danger" size="sm" onClick={onConfirm} disabled={pending}>
          {confirmLabel}
        </Button>
      </div>
    </ResponsiveDialog>
  );
}
