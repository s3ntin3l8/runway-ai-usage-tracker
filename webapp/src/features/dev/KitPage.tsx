// Dev-only design-system gallery (route exists only when import.meta.env.DEV).
// Visual QA surface for the primitives in both themes.

import { useMemo, useState } from 'react';
import { Inbox } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Countdown } from '@/components/ui/Countdown';
import { EmptyState } from '@/components/ui/EmptyState';
import { Gauge } from '@/components/ui/Gauge';
import { HelperText, Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatusDot } from '@/components/ui/StatusDot';
import { Switch } from '@/components/ui/Switch';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { Tooltip, TooltipProvider } from '@/components/ui/Tooltip';
import { EChart } from '@/components/charts/EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from '@/components/charts/theme';
import { useTheme } from '@/hooks/useTheme';
import type { QuotaStatus } from '@/lib/quota';

const STATUSES: QuotaStatus[] = ['critical', 'warning', 'ok', 'unlimited', 'unknown'];

export function KitPage() {
  const { pref, setPref } = useTheme();
  const [dialogOpen, setDialogOpen] = useState(false);
  const inAnHour = useMemo(() => new Date(Date.now() + 3_723_000).toISOString(), []);

  return (
    <TooltipProvider>
      <PageHeader
        title="UI Kit"
        description="Design-system gallery (dev only)"
        actions={
          <Select value={pref} onValueChange={(v) => setPref(v as typeof pref)}>
            <SelectTrigger className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="dark">Dark</SelectItem>
              <SelectItem value="light">Light</SelectItem>
              <SelectItem value="system">System</SelectItem>
            </SelectContent>
          </Select>
        }
      />
      <div className="grid gap-4 p-4 lg:grid-cols-2 lg:p-8">
        <Card>
          <CardHeader>
            <CardTitle>Buttons</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-2">
            <Button variant="primary">Primary</Button>
            <Button>Secondary</Button>
            <Button variant="ghost">Ghost</Button>
            <Button variant="danger">Danger</Button>
            <Button variant="primary" loading>
              Saving
            </Button>
            <Button disabled>Disabled</Button>
            <Button variant="primary" size="lg">
              Mobile CTA
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Badges & status</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3">
            {STATUSES.map((s) => (
              <Badge key={s} variant={s}>
                <StatusDot status={s} /> {s}
              </Badge>
            ))}
            <Badge variant="accent">accent</Badge>
            <Badge variant="outline">Pro tier</Badge>
            <StatusDot status="critical" pulse label="critical, live" />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Gauges</CardTitle>
            <Countdown until={inAnHour} />
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Gauge pct={96} status="critical" size="lg" />
            <Gauge pct={78} status="warning" />
            <Gauge pct={31} status="ok" />
            <Gauge pct={12} status="unlimited" size="sm" />
            <Gauge pct={null} status="unknown" size="sm" />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Form controls</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="kit-input">API key</Label>
              <Input id="kit-input" placeholder="sk-…" />
              <HelperText>Stored encrypted; leave empty to keep the current key.</HelperText>
            </div>
            <div className="flex items-center gap-3">
              <Switch defaultChecked id="kit-switch" />
              <Label htmlFor="kit-switch">Collection enabled</Label>
              <Tooltip content="Pause skips this provider during polling.">
                <Button variant="ghost" size="sm">
                  ?
                </Button>
              </Tooltip>
            </div>
            <Button variant="primary" onClick={() => setDialogOpen(true)} className="self-start">
              Open dialog / sheet
            </Button>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Tabs + table</CardTitle>
          </CardHeader>
          <CardContent>
            <Tabs defaultValue="one">
              <TabsList>
                <TabsTrigger value="one">Overview</TabsTrigger>
                <TabsTrigger value="two">Activity</TabsTrigger>
                <TabsTrigger value="three">Forecast</TabsTrigger>
              </TabsList>
              <TabsContent value="one">
                <Table>
                  <THead>
                    <TR>
                      <TH>Model</TH>
                      <TH className="text-right">Tokens</TH>
                      <TH className="text-right">Cost</TH>
                    </TR>
                  </THead>
                  <TBody>
                    <TR>
                      <TD>claude-sonnet-4-6</TD>
                      <TD className="text-right font-mono tabular">1.24M</TD>
                      <TD className="text-right font-mono tabular">$3.81</TD>
                    </TR>
                    <TR>
                      <TD>claude-opus-4-8</TD>
                      <TD className="text-right font-mono tabular">312K</TD>
                      <TD className="text-right font-mono tabular">$9.12</TD>
                    </TR>
                  </TBody>
                </Table>
              </TabsContent>
              <TabsContent value="two">
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-4 w-2/3" />
                  <Skeleton className="h-4 w-1/3" />
                </div>
              </TabsContent>
              <TabsContent value="three">
                <EmptyState
                  icon={Inbox}
                  title="No forecast yet"
                  description="Needs at least two snapshots in the current window."
                  action={<Button size="sm">Collect now</Button>}
                />
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Chart theme bridge</CardTitle>
          </CardHeader>
          <CardContent>
            <SampleChart />
          </CardContent>
        </Card>
      </div>

      <ResponsiveDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        title="Responsive dialog"
        description="Dialog on desktop, bottom sheet on mobile."
      >
        <p className="text-sm text-fg-muted">
          Resize the window below 1024px and reopen to see the vaul sheet variant.
        </p>
        <Button variant="primary" className="mt-4 w-full" onClick={() => setDialogOpen(false)}>
          Done
        </Button>
      </ResponsiveDialog>
    </TooltipProvider>
  );
}

function SampleChart() {
  const t = useChartTokens();
  const option = useMemo(
    () => ({
      color: t.series,
      tooltip: { trigger: 'axis' as const, ...baseTooltip(t) },
      grid: { left: 40, right: 16, top: 16, bottom: 28 },
      xAxis: {
        type: 'category' as const,
        data: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        ...baseAxisStyle(t),
      },
      yAxis: { type: 'value' as const, ...baseAxisStyle(t) },
      series: [
        { name: 'Claude', type: 'line' as const, smooth: true, data: [31, 42, 38, 55, 72, 68, 81] },
        { name: 'OpenAI', type: 'line' as const, smooth: true, data: [12, 18, 22, 19, 26, 31, 28] },
      ],
    }),
    [t],
  );
  return <EChart option={option} className="h-56" />;
}
