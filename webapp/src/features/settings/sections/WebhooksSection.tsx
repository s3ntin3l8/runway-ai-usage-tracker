// Threshold alerts: Discord/Slack webhooks firing when a provider's quota
// crosses a percentage.

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { BellRing, Plus, Send, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import {
  createWebhook,
  deleteWebhook,
  fetchWebhooks,
  testWebhook,
  updateWebhook,
} from '@/api/endpoints';
import type { Webhook } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Input, Label } from '@/components/ui/Input';
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
import { useProviderConfigs } from '@/features/home/queries';
import { timeAgo } from '@/lib/format';

export function WebhooksSection() {
  const queryClient = useQueryClient();
  const webhooks = useQuery({ queryKey: ['system', 'webhooks'], queryFn: fetchWebhooks });
  const [creating, setCreating] = useState(false);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['system', 'webhooks'] });

  return (
    <div className="flex max-w-2xl flex-col gap-3">
      <div className="flex justify-end">
        <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
          <Plus className="size-3.5" aria-hidden /> Add alert
        </Button>
      </div>

      {webhooks.isPending ? (
        <Skeleton className="h-24" />
      ) : (webhooks.data?.webhooks.length ?? 0) === 0 ? (
        <EmptyState
          icon={BellRing}
          title="No alerts configured"
          description="Get a Discord or Slack message when a provider crosses a usage threshold."
        />
      ) : (
        webhooks.data!.webhooks.map((w) => (
          <WebhookRow key={w.id} webhook={w} onChanged={invalidate} />
        ))
      )}

      <ResponsiveDialog
        open={creating}
        onOpenChange={setCreating}
        title="New alert"
        description="Fires once per crossing; re-arms when usage drops below the threshold."
      >
        <CreateWebhookForm
          onSaved={() => {
            invalidate();
            setCreating(false);
          }}
        />
      </ResponsiveDialog>
    </div>
  );
}

function WebhookRow({ webhook, onChanged }: { webhook: Webhook; onChanged: () => void }) {
  const toggle = useMutation({
    mutationFn: (active: boolean) => updateWebhook(webhook.id, { active }),
    onSuccess: onChanged,
    onError: (err) => toast.error(err.message),
  });
  const test = useMutation({
    mutationFn: () => testWebhook(webhook.id),
    onSuccess: () => toast.success('Test message sent'),
    onError: (err) => toast.error(`Test failed: ${err.message}`),
  });
  const remove = useMutation({
    mutationFn: () => deleteWebhook(webhook.id),
    onSuccess: () => {
      toast.success('Alert deleted');
      onChanged();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card className="flex items-center gap-3 px-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-medium">
          {webhook.provider_id}
          <span className="ml-1.5 font-mono text-fg-muted tabular">
            ≥ {webhook.threshold_pct}%
          </span>
        </p>
        <p className="truncate text-[11px] text-fg-subtle">
          {webhook.channel} ·{' '}
          {webhook.last_fired_at ? `last fired ${timeAgo(webhook.last_fired_at)}` : 'never fired'}
        </p>
      </div>
      <Badge variant={webhook.channel === 'discord' ? 'accent' : 'unlimited'}>
        {webhook.channel}
      </Badge>
      <Switch
        checked={webhook.active}
        onCheckedChange={(on) => toggle.mutate(on)}
        aria-label="Alert active"
      />
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label="Send test"
        title="Send test"
        onClick={() => test.mutate()}
        loading={test.isPending}
      >
        <Send className="size-3.5" />
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label="Delete alert"
        title="Delete alert"
        onClick={() => remove.mutate()}
        loading={remove.isPending}
        className="text-critical hover:bg-critical-muted"
      >
        <Trash2 className="size-3.5" />
      </Button>
    </Card>
  );
}

function CreateWebhookForm({ onSaved }: { onSaved: () => void }) {
  const providers = useProviderConfigs();
  const [providerId, setProviderId] = useState('');
  const [threshold, setThreshold] = useState('80');
  const [url, setUrl] = useState('');
  const [channel, setChannel] = useState<'discord' | 'slack'>('discord');

  const save = useMutation({
    mutationFn: () =>
      createWebhook({
        provider_id: providerId,
        threshold_pct: Number(threshold),
        url: url.trim(),
        channel,
      }),
    onSuccess: () => {
      toast.success('Alert created');
      onSaved();
    },
    onError: (err) => toast.error(err.message),
  });

  const valid = providerId !== '' && url.trim() !== '' && Number(threshold) > 0;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) save.mutate();
      }}
      className="flex flex-col gap-3"
    >
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label>Provider</Label>
          <Select value={providerId} onValueChange={setProviderId}>
            <SelectTrigger>
              <SelectValue placeholder="Select…" />
            </SelectTrigger>
            <SelectContent>
              {(providers.data?.providers ?? []).map((p) => (
                <SelectItem key={p.provider_id} value={p.provider_id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wh-threshold">Threshold (%)</Label>
          <Input
            id="wh-threshold"
            type="number"
            inputMode="numeric"
            min={1}
            max={100}
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="wh-url">Webhook URL</Label>
        <Input
          id="wh-url"
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://discord.com/api/webhooks/…"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label>Channel</Label>
        <Select value={channel} onValueChange={(v) => setChannel(v as 'discord' | 'slack')}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="discord">Discord</SelectItem>
            <SelectItem value="slack">Slack</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <Button type="submit" variant="primary" disabled={!valid} loading={save.isPending}>
        Create alert
      </Button>
    </form>
  );
}
