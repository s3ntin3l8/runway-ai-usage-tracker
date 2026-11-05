import { toast } from 'sonner';
import { deleteProviderConfig } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { errorMessage } from '@/lib/errorMessage';

/**
 * The permanent delete of an empty archived account, with its confirmation.
 * An archived account holds no live credential, so removing the row loses
 * nothing but the account itself. Offered wherever an archived row is listed
 * — including the leftover row of a merged-away account, which is otherwise
 * not restorable.
 */
export function PermanentDeleteButton({
  providerId,
  accountId,
  pendingKey,
  setPendingKey,
  onDeleted,
}: {
  providerId: string;
  accountId: string;
  pendingKey: string | null;
  setPendingKey: (key: string | null) => void;
  onDeleted: () => void;
}) {
  const key = `${providerId}/${accountId}`;
  if (pendingKey === key) {
    return (
      <div className="flex w-full items-center justify-end gap-2 border-t border-border pt-2">
        <span className="mr-auto text-xs text-fg-muted">Permanently delete this empty account?</span>
        <Button variant="ghost" size="sm" onClick={() => setPendingKey(null)}>
          Cancel
        </Button>
        <Button
          variant="danger"
          size="sm"
          onClick={async () => {
            try {
              await deleteProviderConfig(providerId, accountId, true);
              setPendingKey(null);
              onDeleted();
              toast.success('Archived account permanently deleted');
            } catch (error) {
              toast.error(errorMessage(error, 'Could not delete account'));
            }
          }}
        >
          Delete permanently
        </Button>
      </div>
    );
  }
  return (
    <Button variant="danger" size="sm" onClick={() => setPendingKey(key)}>
      Delete permanently
    </Button>
  );
}
