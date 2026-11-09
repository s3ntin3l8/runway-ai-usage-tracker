/** "Free usage applies to: a, b, c" — cloud models the free allowance covers. */
export function FreeModels({ models }: { models?: string[] | null }) {
  if (!models || models.length === 0) return null;
  return (
    <p className="mt-2 text-[11px] text-fg-subtle">Free usage applies to: {models.join(', ')}</p>
  );
}
