// System: global app config (timezone, default poll interval, browser
// preference) and the maintenance actions (force-collect, wake, cleanup).

import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  fetchSettings,
  logout,
  postCleanup,
  postWake,
  putAppConfig,
  revokeAllSessions,
} from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { HelperText, Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import { Skeleton } from '@/components/ui/Skeleton';
import { Switch } from '@/components/ui/Switch';
import { useQuery } from '@tanstack/react-query';
import { fetchAppConfig } from '@/api/endpoints';
import { useForceCollect } from '@/hooks/useForceCollect';
import { setTzConfig } from '@/lib/tz';

export function SystemSection() {
  const queryClient = useQueryClient();
  const appConfig = useQuery({ queryKey: ['system', 'app-config'], queryFn: fetchAppConfig });

  const [timezone, setTimezone] = useState('');
  const [pollInterval, setPollInterval] = useState('');
  const [browserPref, setBrowserPref] = useState('');
  const [channel, setChannel] = useState<'stable' | 'edge'>('stable');
  const [autoUpdate, setAutoUpdate] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);

  useEffect(() => {
    if (!appConfig.data) return;
    setTimezone(appConfig.data.user_timezone ?? '');
    setPollInterval(
      appConfig.data.default_poll_interval_seconds != null
        ? String(appConfig.data.default_poll_interval_seconds)
        : '',
    );
    setBrowserPref(appConfig.data.browser_preference ?? '');
    setChannel(appConfig.data.sidecar_update_channel === 'edge' ? 'edge' : 'stable');
    setAutoUpdate(appConfig.data.sidecar_auto_update === true);
  }, [appConfig.data]);

  const save = useMutation({
    mutationFn: () =>
      putAppConfig({
        user_timezone: timezone.trim() || null,
        default_poll_interval_seconds:
          pollInterval.trim() === '' ? undefined : Number(pollInterval),
        browser_preference: browserPref.trim() || null,
        sidecar_update_channel: channel,
        sidecar_auto_update: autoUpdate,
      }),
    onSuccess: () => {
      toast.success('System settings saved');
      setTzConfig({ user_timezone: timezone.trim() || null });
      queryClient.invalidateQueries({ queryKey: ['system', 'app-config'] });
    },
    onError: (err) => toast.error(err.message),
  });

  const collect = useForceCollect();

  const wake = useMutation({
    mutationFn: postWake,
    onSuccess: () => toast.success('Poller woken'),
  });

  // Drives whether the Session card renders — nothing to sign out of on an
  // open (no admin key) or localhost-trusted instance.
  const settings = useQuery({ queryKey: ['system', 'settings'], queryFn: fetchSettings });

  // Both clear auth, so refetch settings → BootGate re-locks to the key screen.
  const signOut = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      toast.success('Signed out');
      queryClient.invalidateQueries();
    },
    onError: (err) => toast.error(err.message),
  });

  const signOutEverywhere = useMutation({
    mutationFn: revokeAllSessions,
    onSuccess: () => {
      toast.success('All sessions revoked');
      queryClient.invalidateQueries();
    },
    onError: (err) => toast.error(err.message),
  });

  if (appConfig.isPending) return <Skeleton className="h-64 max-w-2xl" />;

  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Configuration</CardTitle>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
            className="flex flex-col gap-4"
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="sys-tz">Timezone (IANA)</Label>
                <Input
                  id="sys-tz"
                  value={timezone}
                  onChange={(e) => setTimezone(e.target.value)}
                  placeholder={appConfig.data?.env_timezone || 'auto-detect'}
                />
                <HelperText>
                  Drives "this month" buckets, heatmaps and chart labels. Empty = TZ env var, then
                  browser.
                </HelperText>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="sys-poll">Default poll interval (s)</Label>
                <Input
                  id="sys-poll"
                  type="number"
                  inputMode="numeric"
                  min={30}
                  value={pollInterval}
                  onChange={(e) => setPollInterval(e.target.value)}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sys-browser">Browser preference</Label>
              <Input
                id="sys-browser"
                value={browserPref}
                onChange={(e) => setBrowserPref(e.target.value)}
                placeholder="e.g. firefox, chrome (cookie extraction)"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sys-channel">Sidecar update channel</Label>
              {/* Guard the spurious empty-string Radix emits when the controlled
                  value updates while the (portalled) items aren't mounted — it
                  was clobbering a loaded "edge" back to "" on every reload. */}
              <Select
                value={channel}
                onValueChange={(v) => {
                  if (v) setChannel(v as 'stable' | 'edge');
                }}
              >
                <SelectTrigger id="sys-channel" className="w-full sm:max-w-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="stable">Stable</SelectItem>
                  <SelectItem value="edge">Edge (rolling prerelease)</SelectItem>
                </SelectContent>
              </Select>
              <HelperText>
                Which release sidecars compare against for the "update available" check. Edge tracks
                the rolling prerelease build.
              </HelperText>
            </div>
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between gap-3">
                <Label htmlFor="sys-auto-update">Auto-install updates</Label>
                <Switch
                  id="sys-auto-update"
                  checked={autoUpdate}
                  onCheckedChange={setAutoUpdate}
                />
              </div>
              <HelperText>
                When on, sidecars self-install available updates on their next check. A sidecar's
                explicit local <code>auto_update</code> config overrides this. Packaged builds only —
                from-source and Docker sidecars never self-update.
              </HelperText>
            </div>
            <Button type="submit" variant="primary" className="self-start" loading={save.isPending}>
              Save
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Maintenance</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Button onClick={() => collect.mutate()} loading={collect.isPending}>
            Force collect
          </Button>
          <Button onClick={() => wake.mutate()} loading={wake.isPending}>
            Wake poller
          </Button>
          <Button variant="danger-ghost" onClick={() => setCleanupOpen(true)}>
            Database cleanup…
          </Button>
        </CardContent>
      </Card>

      {settings.data?.admin_auth_required ? (
        <Card>
          <CardHeader>
            <CardTitle>Session</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {settings.data?.user_context ? (
              // Authenticated via a trusted forward-auth proxy (e.g. Authentik) —
              // there's no local session cookie to sign out of; the proxy owns
              // identity and re-authenticates on the very next request.
              <>
                <p className="text-sm">
                  Signed in as <span className="font-medium">{settings.data.user_context}</span>{' '}
                  via SSO.
                </p>
                <HelperText>
                  This session is authenticated by your forward-auth identity provider — sign out
                  there to sign out of Runway. Local sign-out doesn't apply while a trusted proxy
                  is asserting your identity.
                </HelperText>
              </>
            ) : (
              <>
                <div className="flex flex-wrap gap-2">
                  <Button onClick={() => signOut.mutate()} loading={signOut.isPending}>
                    Sign out
                  </Button>
                  <Button
                    variant="danger-ghost"
                    onClick={() => signOutEverywhere.mutate()}
                    loading={signOutEverywhere.isPending}
                  >
                    Sign out everywhere
                  </Button>
                </div>
                <HelperText>
                  "Sign out" clears this browser's session. "Sign out everywhere" rotates the
                  server session secret, immediately invalidating every signed-in device — use it
                  if a session cookie may be compromised.
                </HelperText>
              </>
            )}
          </CardContent>
        </Card>
      ) : null}

      <CleanupDialog open={cleanupOpen} onOpenChange={setCleanupOpen} />
    </div>
  );
}

function CleanupDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [clearCache, setClearCache] = useState(true);
  const [pruneSnapshots, setPruneSnapshots] = useState('');
  const [removeSidecars, setRemoveSidecars] = useState('');

  const run = useMutation({
    mutationFn: () =>
      postCleanup({
        clear_cache: clearCache,
        prune_snapshots_days: pruneSnapshots.trim() === '' ? null : Number(pruneSnapshots),
        remove_inactive_sidecars_days: removeSidecars.trim() === '' ? null : Number(removeSidecars),
      }),
    onSuccess: (r) => {
      toast.success(`Cleanup done: ${JSON.stringify(r.results)}`);
      onOpenChange(false);
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Database cleanup"
      description="Destructive — pruned rows are gone. Leave a field empty to skip that step."
    >
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <Label htmlFor="cl-cache">Clear collector cache</Label>
          <Switch id="cl-cache" checked={clearCache} onCheckedChange={setClearCache} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="cl-snap">Prune snapshots older than (days)</Label>
          <Input
            id="cl-snap"
            type="number"
            inputMode="numeric"
            min={1}
            value={pruneSnapshots}
            onChange={(e) => setPruneSnapshots(e.target.value)}
            placeholder="skip"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="cl-side">Remove sidecars inactive for (days)</Label>
          <Input
            id="cl-side"
            type="number"
            inputMode="numeric"
            min={1}
            value={removeSidecars}
            onChange={(e) => setRemoveSidecars(e.target.value)}
            placeholder="skip"
          />
        </div>
        <div className="flex justify-end gap-2">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="danger" onClick={() => run.mutate()} loading={run.isPending}>
            Run cleanup
          </Button>
        </div>
      </div>
    </ResponsiveDialog>
  );
}
