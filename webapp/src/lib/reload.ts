/**
 * Reload the current page.
 *
 * A one-line wrapper so a test can observe the reload. `window.location`
 * cannot be replaced in every environment: jsdom running inside a vm context
 * makes it non-configurable, so `vi.stubGlobal('location', ...)` throws
 * "Cannot redefine property: location", and `location.reload` is not writable
 * either -- so `vi.spyOn` is not an option. Going through a module gives the
 * test something it can mock.
 *
 * Only the call sites a test actually exercises need this. A reload that
 * nothing observes can stay a direct `window.location.reload()`.
 */
export function reloadPage(): void {
  window.location.reload();
}
