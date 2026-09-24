// "Pair it" step of the Add-a-sidecar card. Mints a one-time pairing code
// (POST /fleet/pairing-codes) and offers three ways to use it:
//   1. "Open in Runway Sidecar" — the runway-sidecar://pair deep link, which
//      the installed app turns into a confirmation page naming this server;
//   2. the code itself, for the sidecar's Settings → "Pair with a code…";
//   3. a CLI one-liner for headless / Linux hosts.
// The ingest key never appears here; the sidecar fetches it over TLS when it
// redeems the code. While a code is live we poll the sidecar list so the new
// machine's arrival is confirmed in place.

import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Check, Copy, Link2, Terminal } from 'lucide-react';
import { ApiError } from '@/api/client';
import { createPairingCode, fetchSidecars } from '@/api/endpoints';
import type { PairingCode } from '@/api/types';
import { Button, buttonVariants } from '@/components/ui/Button';
import { Countdown } from '@/components/ui/Countdown';

function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      size="icon-sm"
      variant="ghost"
      aria-label={label}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          // Clipboard blocked (insecure origin / permissions): the text is
          // on screen and selectable anyway.
        }
      }}
    >
      {done ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
    </Button>
  );
}

export function PairSidecarPanel() {
  const [pairing, setPairing] = useState<PairingCode | null>(null);
  const [expired, setExpired] = useState(false);
  const knownIds = useRef<Set<string> | null>(null);

  // Poll the fleet while a code is live so the new sidecar shows up here.
  const live = pairing !== null && !expired;
  const sidecars = useQuery({
    queryKey: ['fleet', 'sidecars'],
    queryFn: fetchSidecars,
    refetchInterval: live ? 5_000 : 60_000,
  });
  const ids = (sidecars.data?.sidecars ?? []).map((s) => s.sidecar_id);

  const mint = useMutation({
    mutationFn: () => createPairingCode(window.location.origin),
    onSuccess: (res) => {
      // Baseline = the fleet as it was when this code was minted.
      knownIds.current = new Set(ids);
      setPairing(res);
      setExpired(false);
    },
  });
  const arrived = live ? ids.filter((id) => !knownIds.current?.has(id)) : [];

  useEffect(() => {
    if (!pairing) return;
    const ms = new Date(pairing.expires_at).getTime() - Date.now();
    const id = setTimeout(() => setExpired(true), Math.max(0, ms));
    return () => clearTimeout(id);
  }, [pairing]);

  const errorText =
    mint.error instanceof ApiError && mint.error.status === 503
      ? 'Pairing needs a custom INGEST_API_KEY on the server (ingest is disabled until one is set).'
      : mint.error?.message;

  const cli = pairing
    ? `runway-sidecar-cli --pair ${pairing.server_url} ${pairing.code}`
    : '';

  return (
    <section aria-labelledby="pair-title" className="space-y-2 border-t border-edge pt-4">
      <h3 id="pair-title" className="text-xs font-semibold">
        Pair it with this server
      </h3>
      {!pairing || expired ? (
        <div className="space-y-2">
          <p className="text-xs text-fg-muted">
            {expired
              ? 'That code expired. Generate a new one.'
              : 'Once the sidecar is installed and running, generate a one-time link. It connects the sidecar to this server without copying any keys.'}
          </p>
          <Button size="sm" onClick={() => mint.mutate()} loading={mint.isPending}>
            <Link2 className="size-3.5" aria-hidden />
            Generate pairing link
          </Button>
          {errorText ? <p className="text-xs text-critical">{errorText}</p> : null}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <a
              href={pairing.deep_link}
              className={buttonVariants({ variant: 'primary', size: 'md' })}
            >
              <Link2 className="size-3.5" aria-hidden />
              Open in Runway Sidecar
            </a>
            <Countdown until={pairing.expires_at} prefix="expires in" />
          </div>
          <p className="text-xs text-fg-muted">
            The sidecar shows a confirmation page naming{' '}
            <span className="font-mono text-fg">{pairing.server_url}</span>. Check it and click{' '}
            <span className="text-fg">Pair</span>. Link not opening? In the sidecar, open{' '}
            <span className="text-fg">Settings… → Pair with a code…</span> and enter:
          </p>
          <div className="flex items-center gap-2">
            <code
              className="rounded-sm border border-edge bg-surface-2 px-3 py-1.5 font-mono text-base tracking-widest"
              aria-label="Pairing code"
            >
              {pairing.code}
            </code>
            <CopyButton text={pairing.code} label="Copy pairing code" />
          </div>
          <details>
            <summary className="flex cursor-pointer items-center gap-1 text-xs text-fg-muted hover:text-fg">
              <Terminal className="size-3.5" aria-hidden /> Headless / Linux CLI
            </summary>
            <div className="mt-2 flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded-sm bg-surface-2 px-2 py-1 font-mono text-[11px]">
                {cli}
              </code>
              <CopyButton text={cli} label="Copy CLI command" />
            </div>
          </details>
          <p role="status" className="text-xs">
            {arrived.length > 0 ? (
              <span className="text-ok">✓ {arrived.join(', ')} connected.</span>
            ) : (
              <span className="text-fg-subtle">Waiting for the sidecar to check in…</span>
            )}
          </p>
        </div>
      )}
    </section>
  );
}
