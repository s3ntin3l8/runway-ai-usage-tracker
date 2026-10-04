// "Sign in with GitHub" (OAuth device flow) for the GitHub provider. The server
// stores the resulting token encrypted at rest (`github_oauth.json`) and lists it
// in the credential inventory as a server credential, so it is also evidence for
// the account it authenticates. Restored from the pre-#363 LegacyEditDialog.

import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, ExternalLink, LogIn, LogOut } from 'lucide-react';
import { toast } from 'sonner';
import {
  getGitHubOAuthStatus,
  initGitHubOAuth,
  logoutGitHub,
  pollGitHubOAuth,
} from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { Countdown } from '@/components/ui/Countdown';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';

type FlowState =
  | { phase: 'idle' }
  | {
      phase: 'pending';
      deviceCode: string;
      userCode: string;
      verificationUri: string;
      expiresAt: string;
      pollInterval: number;
    }
  | { phase: 'error'; message: string };

export function GitHubLoginSection() {
  const invalidateCredentialViews = useInvalidateCredentialViews();
  const queryClient = useQueryClient();
  const [flow, setFlow] = useState<FlowState>({ phase: 'idle' });
  const [copied, setCopied] = useState(false);
  const pendingPollRef = useRef<{ stop: () => void } | null>(null);

  const statusQuery = useQuery({
    queryKey: ['github-oauth-status'],
    queryFn: getGitHubOAuthStatus,
    staleTime: 30_000,
    retry: 1,
  });

  useEffect(() => {
    return () => {
      pendingPollRef.current?.stop();
    };
  }, []);

  const invalidateAfterAuth = () => {
    queryClient.invalidateQueries({ queryKey: ['github-oauth-status'] });
    invalidateCredentialViews();
  };

  const startPolling = (
    deviceCode: string,
    initialInterval: number,
  ) => {
    pendingPollRef.current?.stop();
    let stopped = false;
    let pollMs = initialInterval * 1000;
    let timeoutId: ReturnType<typeof setTimeout>;

    const poll = async () => {
      if (stopped) return;
      try {
        const result = await pollGitHubOAuth(deviceCode);
        if (result.status === 'success') {
          stopped = true;
          setFlow({ phase: 'idle' });
          toast.success('GitHub connected');
          invalidateAfterAuth();
          return;
        }
        if (result.status === 'slow_down' && result.interval) {
          pollMs = result.interval * 1000;
        }
      } catch {
        stopped = true;
        setFlow({ phase: 'error', message: 'Authorisation failed — try again' });
        return;
      }
      if (!stopped) timeoutId = setTimeout(poll, pollMs);
    };

    timeoutId = setTimeout(poll, pollMs);
    pendingPollRef.current = {
      stop: () => {
        stopped = true;
        clearTimeout(timeoutId);
      },
    };
  };

  const login = useMutation({
    mutationFn: initGitHubOAuth,
    onSuccess: (data) => {
      const expiresAt = new Date(Date.now() + data.expires_in * 1000).toISOString();
      setFlow({
        phase: 'pending',
        deviceCode: data.device_code,
        userCode: data.user_code,
        verificationUri: data.verification_uri,
        expiresAt,
        pollInterval: data.interval,
      });
      startPolling(data.device_code, data.interval);
    },
    onError: (err) => setFlow({ phase: 'error', message: err.message }),
  });

  const logout = useMutation({
    mutationFn: logoutGitHub,
    onSuccess: () => {
      toast.success('GitHub disconnected');
      invalidateAfterAuth();
    },
    onError: (err) => toast.error(err.message),
  });

  const cancel = () => {
    pendingPollRef.current?.stop();
    setFlow({ phase: 'idle' });
  };

  const copyCode = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.info(`Code: ${code}`);
    }
  };

  if (flow.phase === 'pending') {
    return (
      <div className="rounded-md border border-edge bg-surface-2 p-4">
        <p className="mb-1 text-[12px] text-fg-subtle">
          Open{' '}
          <a
            href={flow.verificationUri}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 text-accent underline underline-offset-2"
          >
            github.com/login/device
            <ExternalLink className="size-2.5" />
          </a>{' '}
          and enter this code:
        </p>
        <div className="mb-3 flex items-center gap-2">
          <code className="flex-1 rounded bg-surface-1 px-3 py-2 font-mono text-xl tracking-[0.25em]">
            {flow.userCode}
          </code>
          <Button
            size="icon-sm"
            variant="ghost"
            onClick={() => void copyCode(flow.userCode)}
            title="Copy code"
          >
            {copied ? (
              <span className="text-[10px] font-bold text-ok">✓</span>
            ) : (
              <Copy className="size-3.5" />
            )}
          </Button>
        </div>
        <div className="flex items-center justify-between">
          <Countdown until={flow.expiresAt} prefix="expires in" />
          <div className="flex items-center gap-2">
            <span className="animate-pulse text-[11px] text-fg-muted">Waiting…</span>
            <Button size="sm" variant="ghost" onClick={cancel}>
              Cancel
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (flow.phase === 'error') {
    return (
      <div className="rounded-md border border-critical/30 bg-critical/5 p-3">
        <p className="mb-2 text-[12px] text-critical">{flow.message}</p>
        <Button size="sm" variant="ghost" onClick={() => setFlow({ phase: 'idle' })}>
          Try again
        </Button>
      </div>
    );
  }

  const auth = statusQuery.data;

  if (auth?.authenticated) {
    return (
      <div className="flex items-center gap-3 rounded-md border border-edge bg-surface-2 px-3 py-2.5">
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-medium">
            {auth.account ? `@${auth.account}` : 'Connected'}
          </p>
          {auth.email ? (
            <p className="truncate text-[11px] text-fg-subtle">{auth.email}</p>
          ) : null}
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => logout.mutate()}
          loading={logout.isPending}
          className="shrink-0 text-fg-muted"
        >
          <LogOut className="mr-1 size-3" />
          Disconnect
        </Button>
      </div>
    );
  }

  return (
    <Button
      variant="secondary"
      size="sm"
      className="w-full justify-center"
      onClick={() => login.mutate()}
      loading={login.isPending || statusQuery.isPending}
    >
      <LogIn className="mr-1.5 size-3.5" />
      Connect via GitHub OAuth
    </Button>
  );
}
