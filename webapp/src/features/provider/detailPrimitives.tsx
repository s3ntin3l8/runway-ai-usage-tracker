// Small framed-detail primitives shared by the Activity "Top sessions" detail
// panel (SessionsTable) and the Cost "by model/sidecar" expandable rows
// (CostTab), so the two expand-to-detail views stay visually consistent.

/** Labelled token/number/cost chip used across detail panels. */
export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] tracking-wide text-fg-subtle uppercase">{label}</span>
      <span className="font-mono text-xs tabular text-fg">{value}</span>
    </div>
  );
}

/** Titled, framed section grouping a set of Stats / cards. */
export function DetailSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[11px] font-medium text-fg-muted">{title}</span>
      <div className="rounded-md border border-edge bg-surface-1 p-3">{children}</div>
    </div>
  );
}
