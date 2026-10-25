import type { LucideIcon } from 'lucide-react';

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  /**
   * The heading level for the title.
   *
   * The title used to be a `<p>`, so a screen-reader user could not jump to
   * it. Making it a heading means the LEVEL matters: the axe guard enforces
   * `heading-order`, and a heading that skips a level (an `h1` followed by
   * an `h3`) is a violation.
   *
   * The default is `h2` because it never skips a level — it is safe under a
   * page heading (`h1`) and under a card title (`h2`) alike. Pass `h3` for a
   * state that sits *inside* a card whose own title is an `h2`.
   */
  headingLevel?: 'h2' | 'h3' | 'h4';
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  headingLevel = 'h2',
}: EmptyStateProps) {
  const Heading = headingLevel;
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
      {Icon ? <Icon className="size-8 text-fg-subtle" aria-hidden /> : null}
      <Heading className="text-sm font-medium">{title}</Heading>
      {description ? <p className="max-w-sm text-xs text-fg-muted">{description}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
