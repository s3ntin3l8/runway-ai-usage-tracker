import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AppConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { patternError } from '@/lib/worktreePatterns';
import { WorktreePatternsCard } from './WorktreePatternsCard';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const cfg = (o: Partial<AppConfig> = {}): AppConfig => ({
  worktree_patterns: ['*-wt'],
  worktree_builtin_patterns: ['.claude', '.wt'],
  ...o,
});

describe('patternError', () => {
  it.each([
    ['', /enter/i],
    ['a/b', /separator/i],
    ['a\\b', /separator/i],
    ['*', /literal/i],
    ['a?', /wildcard/i],
    ['x'.repeat(65), /characters/i],
    ['*-WT', /already/i],
  ])('rejects %j', (value, message) => {
    expect(patternError(value, ['*-wt'])).toMatch(message);
  });

  it('accepts a good pattern and caps the list', () => {
    expect(patternError(' wt-* ', [])).toBeNull();
    expect(patternError('new', Array.from({ length: 20 }, (_, i) => `p${i}`))).toMatch(/at most/i);
  });
});

describe('WorktreePatternsCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('lists saved patterns, adds and removes, and saves the list', async () => {
    vi.mocked(api.putAppConfig).mockResolvedValue({ status: 'saved' });
    renderWithProviders(<WorktreePatternsCard config={cfg()} />);

    expect(await screen.findByRole('button', { name: 'Remove *-wt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save patterns/i })).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/extra worktree directories/i), 'extra{Enter}');
    expect(screen.getByText('extra')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Remove *-wt' }));
    await userEvent.click(screen.getByRole('button', { name: /save patterns/i }));

    expect(api.putAppConfig).toHaveBeenCalledWith({ worktree_patterns: ['extra'] });
  });

  it('shows an inline error for an invalid entry and does not add it', async () => {
    renderWithProviders(<WorktreePatternsCard config={cfg()} />);

    await userEvent.type(screen.getByLabelText(/extra worktree directories/i), 'a/b');
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }));

    expect(screen.getByRole('alert')).toHaveTextContent(/separator/i);
    expect(screen.getByLabelText(/extra worktree directories/i)).toHaveAttribute(
      'aria-describedby',
      'sys-wt-pattern-error',
    );
    const list = screen.getByRole('list', { name: 'Worktree patterns' });
    expect(list.querySelectorAll('li')).toHaveLength(1);
  });
});
