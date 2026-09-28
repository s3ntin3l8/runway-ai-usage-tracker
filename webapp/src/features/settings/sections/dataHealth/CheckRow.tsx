// One check's row: severity badge, total/fixable counts, and an expandable
// list of its finding groups. Each fixable group gets a "Fix" button opening
// FixDialog; a not-fixable group shows its reason instead (e.g. opencode-byok
// has no configured account to reassign lone default events onto).

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Link } from 'react-router';
import type {
  DataHealthCheckReport,
  DataHealthFindingGroup,
  DataHealthSeverity,
} from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { cn } from '@/lib/cn';
import { FixDialog } from './FixDialog';
import { SampleTable } from './SampleTable';

interface SeverityBadgeInfo {
  variant: 'critical' | 'warning' | 'neutral';
  label: string;
}

const SEVERITY_BADGE: Record<DataHealthSeverity, SeverityBadgeInfo> = {
  error: { variant: 'critical', label: 'Error' },
  warn: { variant: 'warning', label: 'Warning' },
  info: { variant: 'neutral', label: 'Info' },
};

export function CheckRow({
  check,
  checkNames = {},
  stale = false,
}: {
  check: DataHealthCheckReport;
  checkNames?: Record<string, string>;
  stale?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const severity = SEVERITY_BADGE[check.severity];

  return (
    <Card className="flex flex-col gap-2 p-3">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="flex w-full items-center gap-2.5 text-left"
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
        )}
        <Badge variant={severity.variant}>{severity.label}</Badge>
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg">
          {check.title}
        </span>
        {check.blocked ? <Badge variant="outline">Blocked</Badge> : null}
        <span className="text-[12px] tabular text-fg-subtle">
          {check.total_count === 0
            ? 'clean'
            : `${check.total_count.toLocaleString()} ${check.total_count === 1 ? 'finding' : 'findings'}`}
        </span>
      </button>
      <p className="pl-6 text-[12px] text-fg-subtle">{check.description}</p>

      {expanded && (
        <div className="flex flex-col gap-2 border-t border-edge pt-2 pl-6">
          <p className="text-[12px] text-fg-subtle">{check.impact}</p>
          <p className="text-[12px] text-fg">
            <span className="font-medium">Next step:</span> {check.recommended_action}
          </p>
          {check.blocked_by.length > 0 && (
            <p className="text-[12px] text-warning">
              Fix first: {check.blocked_by.map((id) => checkNames[id] ?? id).join(', ')}.
            </p>
          )}
          {check.groups.length > 0 ? (
            check.groups.map((group) => (
              <GroupRow
                key={group.key}
                checkId={check.check_id}
                group={group}
                blocked={check.blocked || stale}
              />
            ))
          ) : (
            <p className="text-[12px] text-fg-subtle">No findings — this check is clean.</p>
          )}
        </div>
      )}
    </Card>
  );
}

function GroupRow({
  checkId,
  group,
  blocked,
}: {
  checkId: string;
  group: DataHealthFindingGroup;
  blocked: boolean;
}) {
  const [showSamples, setShowSamples] = useState(false);
  const [fixing, setFixing] = useState(false);
  const link = typeof group.detail.link === 'string' ? group.detail.link : null;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-[12px] text-fg">{group.label}</span>
        {group.samples.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => setShowSamples((s) => !s)}>
            {showSamples ? 'Hide samples' : 'Show samples'}
          </Button>
        )}
        {group.fixable ? (
          <Button size="sm" variant="secondary" onClick={() => setFixing(true)} disabled={blocked}>
            Fix
          </Button>
        ) : (
          <span className="flex items-center gap-2">
            <span
              className={cn('text-[11px] text-fg-subtle', 'max-w-64 truncate')}
              title={group.not_fixable_reason ?? undefined}
            >
              {group.not_fixable_reason ?? 'not fixable'}
            </span>
            {link && (
              <Link className="text-[11px] font-medium text-accent underline" to={link}>
                Open Fleet
              </Link>
            )}
          </span>
        )}
      </div>
      {showSamples && <SampleTable samples={group.samples} />}
      {fixing && (
        <FixDialog open={fixing} onOpenChange={setFixing} checkId={checkId} group={group} />
      )}
    </div>
  );
}
