// Global vitest setup for component tests (jsdom environment).
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

// English date-label assertions must not depend on the developer's OS locale.
// Explicit locales still reach the native implementation; product code remains localized.
// toLocaleString and toLocaleTimeString are pinned for the same reason: the
// timestamp helpers render through them, so an unpinned call makes a
// fixed instant assert differently on two machines.
const nativeLocaleDate = Date.prototype.toLocaleDateString;
const nativeLocaleTime = Date.prototype.toLocaleTimeString;
const nativeLocaleString = Date.prototype.toLocaleString;

type LocaleFn = (
  this: Date,
  locales?: Intl.LocalesArgument,
  options?: Intl.DateTimeFormatOptions,
) => string;

function pinDefaultLocale(native: LocaleFn): LocaleFn {
  return function (this: Date, locales, options) {
    const locale = locales == null || (Array.isArray(locales) && locales.length === 0)
      ? 'en-US'
      : locales;
    return native.call(this, locale, options);
  };
}

beforeEach(() => {
  vi.spyOn(Date.prototype, 'toLocaleDateString').mockImplementation(pinDefaultLocale(nativeLocaleDate));
  vi.spyOn(Date.prototype, 'toLocaleTimeString').mockImplementation(pinDefaultLocale(nativeLocaleTime));
  vi.spyOn(Date.prototype, 'toLocaleString').mockImplementation(pinDefaultLocale(nativeLocaleString));
});
afterEach(() => vi.restoreAllMocks());

// RTL auto-cleanup between tests.
afterEach(() => cleanup());

// Node 22+ ships a native `localStorage`/`sessionStorage` global (Web Storage
// API) that is `undefined` unless `--localstorage-file` is passed, and it's an
// own accessor on globalThis that shadows the one jsdom installs. On Node 26
// (no flag) every test that touches storage — e.g. ExcludeCacheProvider's
// useState initializer — throws. Install an in-memory Storage only when the
// global one is unusable, so CI (where jsdom provides a working store) is
// unaffected.
function installMemoryStorage(name: 'localStorage' | 'sessionStorage') {
  const works = (() => {
    try {
      const s = (globalThis as unknown as Record<string, Storage | undefined>)[name];
      if (!s) return false;
      s.setItem('__probe__', '1');
      s.removeItem('__probe__');
      return true;
    } catch {
      return false;
    }
  })();
  if (works) return;

  const store = new Map<string, string>();
  const mock: Storage = {
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    setItem: (k, v) => void store.set(String(k), String(v)),
    removeItem: (k) => void store.delete(String(k)),
    clear: () => store.clear(),
    key: (i) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, name, { value: mock, configurable: true, writable: true });
}

installMemoryStorage('localStorage');
installMemoryStorage('sessionStorage');

// jsdom doesn't implement these browser APIs that Radix / ECharts / hooks touch.
if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

// The observer mocks take the callback the real API takes. A default
// constructor would not: CodeQL reads `new ResizeObserver(update)` against
// this class and reports the callback as a superfluous argument, because a
// class with no constructor of its own takes none. The callback is not kept
// -- the mock never fires -- but it is accepted, so the mock's shape
// matches the API the code under test calls.
class _ResizeObserver {
  constructor(callback: ResizeObserverCallback) {
    void callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= _ResizeObserver as unknown as typeof ResizeObserver;
globalThis.IntersectionObserver ??= class {
  constructor(callback: IntersectionObserverCallback) {
    void callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
  root = null;
  rootMargin = '';
  thresholds = [];
} as unknown as typeof IntersectionObserver;

// Radix pointer-based components call these in jsdom.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
