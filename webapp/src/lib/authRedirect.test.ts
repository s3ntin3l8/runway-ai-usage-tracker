import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { createAuthRedirectGuard } from './authRedirect';

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
