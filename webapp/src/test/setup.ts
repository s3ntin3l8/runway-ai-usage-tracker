// Global vitest setup for component tests (jsdom environment).
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

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

class _ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= _ResizeObserver as unknown as typeof ResizeObserver;
globalThis.IntersectionObserver ??= class {
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
