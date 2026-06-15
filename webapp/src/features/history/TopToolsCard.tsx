// Top Tools card: most-used tools by invocation count over the selected range.
// Sourced from per-event tool names (Anthropic today), so it reflects Claude
// tool usage — labelled as such to avoid implying every provider contributes.

import type { TopToolEntry } from '@/api/types';
import { RankBar, type RankRow } from '@/components/charts/RankBar';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { useTopTools } from './queries';

function toolRow(t: TopToolEntry): RankRow {
  return { label: t.tool, value: t.calls, sub: `${t.msgs.toLocaleString()} msgs` };
}

export function TopToolsCard({ days }: { days: number }) {
  const top = useTopTools(days);
  const rows = (top.data?.tools ?? []).map(toolRow);
  const hasData = rows.some((r) => r.value > 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Top tools · {days}d</CardTitle>
        <span className="text-[11px] text-fg-subtle">Claude tool use</span>
      </CardHeader>
      <CardContent className="pt-2">
        {top.isPending ? (
          <Skeleton className="h-72 w-full" />
        ) : !hasData ? (
          <p className="py-16 text-center text-xs text-fg-subtle">No tool usage in this range.</p>
        ) : (
          <RankBar rows={rows} format={(v) => v.toLocaleString()} />
        )}
      </CardContent>
    </Card>
  );
}
