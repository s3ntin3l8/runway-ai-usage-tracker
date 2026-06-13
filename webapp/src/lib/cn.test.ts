import { cn } from './cn';

describe('cn', () => {
  it('joins truthy class values and skips falsy ones', () => {
    expect(cn('a', false && 'b', null, undefined, 'c')).toBe('a c');
  });

  it('merges conflicting tailwind classes, last one wins', () => {
    expect(cn('px-2', 'px-4')).toBe('px-4');
  });
});
