import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryCache, QueryClient } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import {
  clearAuthReloadCount,
  createAuthRedirectGuard,
  createQueryErrorAuthRedirectHandler,
  SKIP_AUTH_REDIRECT_GUARD_META,
} from './authRedirect';

beforeEach(() => sessionStorage.clear());

describe('createAuthRedirectGuard', () => {
  it('fires once when an authRedirect ApiError is seen', () => {
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new ApiError(0, 'Authentication required', true));
    expect(onExpire).toHaveBeenCalledTimes(1);
  });

  it('does not fire again for later errors, authRedirect or not', () => {
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new ApiError(0, 'Authentication required', true));
    guard(new ApiError(0, 'Authentication required', true));
    guard(new ApiError(500, 'boom'));
    expect(onExpire).toHaveBeenCalledTimes(1);
  });

  it('ignores a plain outage (authRedirect false)', () => {
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new ApiError(0, 'Network error'));
    guard(new ApiError(500, 'boom'));
    expect(onExpire).not.toHaveBeenCalled();
  });

  it('ignores a non-ApiError value', () => {
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new Error('unrelated'));
    guard('not even an error');
    expect(onExpire).not.toHaveBeenCalled();
  });
});

// Reproduces the exact interaction bug flagged in review: BootGate's boot-time
// settings probe and app.tsx's global guard used to share one QueryCache with
// no distinction, so a cold-boot authRedirect both flashed BootGate's own
// "Session expired" card AND fired the global toast+reload ~1.5s later,
// racing that card's "Sign in" button. These use a real QueryClient/QueryCache
// (not a stubbed query object) to prove the wiring, not just the function.
describe('createQueryErrorAuthRedirectHandler', () => {
  const authFailure = () => Promise.reject(new ApiError(0, 'Authentication required', true));

  function setup() {
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    const queryClient = new QueryClient({
      queryCache: new QueryCache({ onError: createQueryErrorAuthRedirectHandler(guard) }),
      defaultOptions: { queries: { retry: false } },
    });
    return { onExpire, queryClient };
  }

  it("skips BootGate's boot probe (flagged with SKIP_AUTH_REDIRECT_GUARD_META)", async () => {
    const { onExpire, queryClient } = setup();
    await queryClient
      .fetchQuery({
        queryKey: ['system', 'settings'],
        queryFn: authFailure,
        meta: SKIP_AUTH_REDIRECT_GUARD_META,
      })
      .catch(() => {});
    expect(onExpire).not.toHaveBeenCalled();
  });

  it('still guards an unflagged query, e.g. a dashboard card failing mid-session', async () => {
    const { onExpire, queryClient } = setup();
    await queryClient
      .fetchQuery({ queryKey: ['usage', 'limits'], queryFn: authFailure })
      .catch(() => {});
    expect(onExpire).toHaveBeenCalledTimes(1);
  });

  it('a skipped boot-probe failure does not consume the fire-once guard for a later real query', async () => {
    const { onExpire, queryClient } = setup();
    await queryClient
      .fetchQuery({
        queryKey: ['system', 'settings'],
        queryFn: authFailure,
        meta: SKIP_AUTH_REDIRECT_GUARD_META,
      })
      .catch(() => {});
    await queryClient
      .fetchQuery({ queryKey: ['usage', 'limits'], queryFn: authFailure })
      .catch(() => {});
    expect(onExpire).toHaveBeenCalledTimes(1);
  });
});

// Each call to createAuthRedirectGuard simulates a new page load (module-level
// constant in app.tsx, recreated on every full page reload). The sessionStorage
// counter persists across these reloads.
describe('reload-loop circuit breaker', () => {
  it('fires onExpire on the first two page loads (under the max)', () => {
    for (let i = 0; i < 2; i++) {
      const onExpire = vi.fn();
      const guard = createAuthRedirectGuard(onExpire);
      guard(new ApiError(0, 'Authentication required', true));
      expect(onExpire).toHaveBeenCalledTimes(1);
    }
    expect(sessionStorage.getItem('runway_auth_reload_count')).toBe('2');
  });

  it('stops firing after the max consecutive reload attempts', () => {
    // Simulate 2 previous page loads that each incremented the counter.
    sessionStorage.setItem('runway_auth_reload_count', '2');
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new ApiError(0, 'Authentication required', true));
    expect(onExpire).not.toHaveBeenCalled();
  });

  it('resets after clearAuthReloadCount, allowing onExpire to fire again', () => {
    sessionStorage.setItem('runway_auth_reload_count', '2');
    clearAuthReloadCount();

    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new ApiError(0, 'Authentication required', true));
    expect(onExpire).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem('runway_auth_reload_count')).toBe('1');
  });

  it('does not increment the counter for non-authRedirect errors', () => {
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new ApiError(500, 'boom'));
    expect(onExpire).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('runway_auth_reload_count')).toBeNull();
  });

  it('handles malformed non-numeric sessionStorage values gracefully', () => {
    sessionStorage.setItem('runway_auth_reload_count', 'invalid-string');
    const onExpire = vi.fn();
    const guard = createAuthRedirectGuard(onExpire);
    guard(new ApiError(0, 'Authentication required', true));
    expect(onExpire).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem('runway_auth_reload_count')).toBe('1');
  });
});
