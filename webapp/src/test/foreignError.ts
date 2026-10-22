/**
 * An error shaped like one that came from another realm.
 *
 * `instanceof` walks the prototype chain, so an error whose chain no longer
 * reaches `Error.prototype` is not `instanceof Error` — which is exactly what
 * an error created in another realm looks like from here. Vitest's
 * `vmThreads` pool runs the test code in a separate realm, so an error the
 * host creates (the `SyntaxError` that `Response.json()` throws on a
 * malformed body) is *not* `instanceof Error` in the test, while it is in the
 * browser.
 *
 * The `message` is intact, because that is the part a caller can actually
 * use.
 */
export function foreignError(message: string): Error {
  const error = new Error(message);
  Object.setPrototypeOf(error, { constructor: Error, name: 'Error' });
  return error;
}
