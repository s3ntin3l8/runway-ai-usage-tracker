/**
 * Reload the current page.
 *
 * A one-line wrapper so the reload is interceptable in a test. `window.location`
 * cannot be replaced in every environment: jsdom running inside a vm context
 * makes it non-configurable, so `vi.stubGlobal('location', ...)` throws
 * "Cannot redefine property: location", and `location.reload` is not writable
 * either -- so `vi.spyOn` is not an option. Going through a module gives the
 * test something it can mock.
 */
export function reloadPage(): void {
  window.location.reload();
}
