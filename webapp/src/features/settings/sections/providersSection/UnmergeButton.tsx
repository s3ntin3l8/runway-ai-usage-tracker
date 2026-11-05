import { useState } from 'react';
import { toast } from 'sonner';
import { previewAccountUnmerge, unmergeProviderAccount } from '@/api/endpoints';
import type { UnmergePlan } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { errorMessage } from '@/lib/errorMessage';
import { unmergeSummary } from './unmergeSummary';

/**
 * Undo a merge: the account's history, its credential and its name go back to
 * the account they came from.
 *
 * The preview states what goes back and what does not — the duplicates the
 * merge dropped and the stored credential it wiped are gone for good — and the
 * operator reads that before confirming.
 */
export function UnmergeButton({
  providerId,
  accountId,
  onUnmerged,
}: {
  providerId: string;
  accountId: string;
  onUnmerged: () => void;
}) {
  const [plan, setPlan] = useState<UnmergePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [acknowledge, setAcknowledge] = useState(false);
  // The account is an account again: the fleet, the credential inventory and
  // the provider cards all read that state, so they refresh together.
  const invalidateCredentialViews = useInvalidateCredentialViews();

  const openPreview = async () => {
    setOpen(true);
    setPlan(null);
    setError(null);
    setAcknowledge(false);
    try {
      setPlan(await previewAccountUnmerge(providerId, accountId));
    } catch (previewError) {
      // A merge that cannot be reversed says so here, in place — the operator
      // reads why instead of getting a toast and an empty panel.
      setError(errorMessage(previewError, 'Could not read what the un-merge would do'));
    }
  };

  const run = async () => {
    setBusy(true);
    try {
      // The count the preview showed goes with the request: the server
      // refuses when the rows in the way changed since, because the operator
      // confirmed a different set of rows.
      await unmergeProviderAccount(
        providerId,
        accountId,
        acknowledge,
        plan?.total_collisions ?? 0,
      );
      toast.success('Account unmerged');
      setOpen(false);
      invalidateCredentialViews();
      onUnmerged();
    } catch (unmergeError) {
      toast.error(errorMessage(unmergeError, 'Could not un-merge account'));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={openPreview}>
        Unmerge
      </Button>
    );
  }

  const blocked = (plan?.total_collisions ?? 0) > 0;
  return (
    <div className="flex w-full flex-col gap-2 border-t border-border pt-2">
      {error ? (
        <p className="text-xs text-fg-muted">{error}</p>
      ) : plan ? (
        <>
          <p className="text-xs text-fg-muted">{unmergeSummary(plan)}</p>
          {plan.not_restored.length > 0 ? (
            <p className="text-xs text-fg-muted">
              Not restored: {plan.not_restored.join('; ')}.
            </p>
          ) : null}
          {blocked ? (
            <label className="flex items-center gap-2 text-xs text-fg-muted">
              <input
                type="checkbox"
                checked={acknowledge}
                onChange={(event) => setAcknowledge(event.target.checked)}
              />
              {plan.total_collisions} row{plan.total_collisions === 1 ? '' : 's'} that got in the
              way {plan.total_collisions === 1 ? 'is' : 'are'} replaced by the rows the merge
              moved.
            </label>
          ) : null}
        </>
      ) : (
        <p className="text-xs text-fg-muted">Reading what the un-merge would do…</p>
      )}
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
        {error ? null : (
          <Button
            variant="primary"
            size="sm"
            loading={busy}
            disabled={!plan || (blocked && !acknowledge)}
            onClick={run}
          >
            Unmerge
          </Button>
        )}
      </div>
    </div>
  );
}
