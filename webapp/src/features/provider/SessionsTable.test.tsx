import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { SessionsTable } from './SessionsTable';
import { session } from './test-fixtures';

describe('SessionsTable', () => {
  it('renders a collapsed row per session', () => {
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({ session_id: 'aaaaaaaa1111', models: ['claude-opus'] }),
          session({ session_id: 'bbbbbbbb2222', models: ['claude-sonnet'] }),
        ]}
      />,
    );
    expect(screen.getByText('aaaaaaaa')).toBeInTheDocument();
    expect(screen.getByText('bbbbbbbb')).toBeInTheDocument();
  });

  it('expands a row to reveal the token breakdown', async () => {
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({
            session_id: 'aaaaaaaa1111',
            tokens_input: 8000,
            tokens_output: 3000,
            tool_calls: 12,
            by_model: [
              {
                model_id: 'claude-opus',
                msgs: 8,
                tokens_input: 8000,
                tokens_output: 3000,
                tokens_total: 11000,
                cost_usd: 1.0,
                tool_calls: 10,
              },
            ],
            subagents: [
              {
                subagent_type: 'Explore',
                turns: 3,
                tool_calls: 5,
                tokens_total: 2000,
                cost_usd: 0.2,
              },
            ],
          }),
        ]}
      />,
    );
    const row = screen.getByText('aaaaaaaa').closest('tr')!;
    expect(row).toHaveAttribute('aria-expanded', 'false');

    await userEvent.click(row);
    expect(row).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Token breakdown')).toBeInTheDocument();
    expect(screen.getByText('By model')).toBeInTheDocument();
    expect(screen.getByText('Explore')).toBeInTheDocument();
  });

  it('drops cache tokens from the Tokens column when excludeCache is set', () => {
    const s = session({
      session_id: 'dddddddd4444',
      subagents: [],
      tokens_total: 12000,
      tokens_input: 2000,
      tokens_output: 1000,
      tokens_cache_read: 6000,
      tokens_cache_create: 3000,
    });
    const { rerender } = renderWithProviders(<SessionsTable sessions={[s]} />);
    // Full total: 12000 → "12K".
    expect(screen.getByText('12K')).toBeInTheDocument();

    rerender(<SessionsTable sessions={[s]} excludeCache />);
    // 12000 − (6000 + 3000) cache = 3000 → "3K".
    expect(screen.getByText('3K')).toBeInTheDocument();
    expect(screen.queryByText('12K')).not.toBeInTheDocument();
  });

  it('notes the absence of subagents in a main-only session', async () => {
    renderWithProviders(
      <SessionsTable sessions={[session({ session_id: 'cccccccc3333', subagents: [] })]} />,
    );
    await userEvent.click(screen.getByText('cccccccc').closest('tr')!);
    expect(screen.getByText(/main session only/i)).toBeInTheDocument();
  });
});
