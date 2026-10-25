/**
 * The message of a thrown value, or a fallback when it carries none.
 *
 * `instanceof Error` is not reliable across realms. A test pool that runs the
 * test code in a separate realm — Vitest's `vmThreads` does — sees host-created
 * errors as *not* `instanceof Error`: the `SyntaxError` that `Response.json()`
 * throws on a malformed body is created by the host, so the check is false in
 * the test realm and true in the browser. A check that answers differently in
 * the two places means the tests exercise a branch the browser never takes.
 *
 * Reading the `message` property answers the same in every realm, so that is
 * what this does. It also covers a rejected promise carrying a plain object
 * with a `message`, which the `instanceof` check silently discarded.
 *
 * The fallback lives here rather than at each call site, so a call site is a
 * single statement with no branch of its own to cover.
 */
export function errorMessage(value: unknown, fallback: string): string {
  if (typeof value === 'object' && value !== null) {
    const message = (value as { message?: unknown }).message;
    // An empty message is no message. A thrown value that says nothing still
    // has to render as *something*: a caller that gates an error state on a
    // truthy string would otherwise fall through to the empty state, which
    // is the failure this helper exists to prevent.
    if (typeof message === 'string' && message.trim() !== '') return message;
  }
  return fallback;
}
