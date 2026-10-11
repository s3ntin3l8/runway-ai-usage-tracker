import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router';
import { KeyRound, X } from 'lucide-react';
import { toast } from 'sonner';
import { dismissCredentialFileMigrationNotice } from '@/api/endpoints';
import type { CredentialInventory } from '@/api/types';
import { credentialInventoryKey } from '@/hooks/useInvalidateCredentialViews';

export function CredentialFileMigrationNotice({
  inventory,
}: {
  inventory: CredentialInventory | undefined;
}) {
  const client = useQueryClient();
  const dismiss = useMutation({
    mutationFn: dismissCredentialFileMigrationNotice,
    onSuccess: () => {
      client.setQueryData<CredentialInventory>(credentialInventoryKey, (current) =>
        current ? { ...current, credential_file_migration_notice: null } : current,
      );
      client.invalidateQueries({ queryKey: credentialInventoryKey });
    },
    onError: (error: Error) => toast.error(`Could not dismiss notice: ${error.message}`),
  });
  const notice = inventory?.credential_file_migration_notice;
  if (!notice) return null;
  return (
    <div
      role="status"
      className="flex items-start gap-2.5 rounded-md border border-warning/30 bg-warning-muted px-4 py-2.5 text-[13px] text-warning"
    >
      <KeyRound className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="flex-1">
        Runway removed legacy server login-file sources for{' '}
        {notice.providers.map((p) => p.name).join(', ')}. To collect these CLI logins, pair a
        sidecar on the machine where you sign in.{' '}
        <Link to="/fleet" className="font-medium underline underline-offset-2">
          Set up a sidecar
        </Link>{' '}
        or{' '}
        <Link to="/settings/providers" className="font-medium underline underline-offset-2">
          add a key in Providers
        </Link>
        .
      </div>
      <button
        type="button"
        aria-label="Dismiss login-file migration notice"
        disabled={dismiss.isPending}
        onClick={() => dismiss.mutate()}
        className="-m-1 shrink-0 cursor-pointer rounded-sm p-1 opacity-70 hover:opacity-100 disabled:opacity-40"
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
