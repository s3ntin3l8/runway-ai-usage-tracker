import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { SessionsTable } from './SessionsTable';
import { session } from './test-fixtures';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

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
    const toggle = within(row).getByRole('button', { name: /expand details for session aaaaaaaa/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await userEvent.click(row);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
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

  it('shows a per-category cost line in the expanded token breakdown', async () => {
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({
            session_id: 'eeeeeeee5555',
            subagents: [],
            cost_usd: 1.0,
            cost_input: 0.4,
            cost_output: 0.3,
            cost_cache_read: 0.2,
            cost_cache_create: 0.1,
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText('eeeeeeee').closest('tr')!);
    expect(screen.getByText('Input $')).toBeInTheDocument();
    expect(screen.getByText('Cache read $')).toBeInTheDocument();
    expect(screen.getByText('Total $')).toBeInTheDocument();
  });

  it('hides cache cost cells in the breakdown when excludeCache is set', async () => {
    const s = session({
      session_id: 'ffffffff6666',
      subagents: [],
      cost_usd: 1.0,
      cost_cache_read: 0.2,
      cost_cache_create: 0.1,
    });
    renderWithProviders(<SessionsTable sessions={[s]} excludeCache />);
    await userEvent.click(screen.getByText('ffffffff').closest('tr')!);
    expect(screen.getByText('Input $')).toBeInTheDocument();
    expect(screen.queryByText('Cache read $')).not.toBeInTheDocument();
    expect(screen.queryByText('Cache write $')).not.toBeInTheDocument();
  });

  it('notes the absence of subagents in a main-only session', async () => {
    renderWithProviders(
      <SessionsTable sessions={[session({ session_id: 'cccccccc3333', subagents: [] })]} />,
    );
    await userEvent.click(screen.getByText('cccccccc').closest('tr')!);
    expect(screen.getByText(/main session only/i)).toBeInTheDocument();
  });
});

describe('SessionsTable expand control', () => {
  it('exposes the expander as a focusable button that owns the expanded state', () => {
    renderWithProviders(<SessionsTable sessions={[session({ session_id: 'aaaaaaaa1111' })]} />);
    const row = screen.getByText('aaaaaaaa').closest('tr')!;
    const toggle = within(row).getByRole('button', {
      name: /expand details for session aaaaaaaa/i,
    });

    expect(toggle.tagName).toBe('BUTTON');
    expect(toggle).toHaveAttribute('type', 'button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    // The state belongs to the control, not to the (unfocusable) row.
    expect(row).not.toHaveAttribute('aria-expanded');
    // The global :focus-visible ring must stay visible on the control.
    expect(toggle.className).not.toMatch(/outline-none/);
  });

  it('reaches the expander with Tab and toggles it with Enter and Space', async () => {
    renderWithProviders(<SessionsTable sessions={[session({ session_id: 'aaaaaaaa1111' })]} />);
    const toggle = screen.getByRole('button', {
      name: /expand details for session aaaaaaaa/i,
    });

    await userEvent.tab();
    expect(toggle).toHaveFocus();

    await userEvent.keyboard('{Enter}');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Token breakdown')).toBeInTheDocument();

    await userEvent.keyboard(' ');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Token breakdown')).not.toBeInTheDocument();
  });

  it('toggles once when the expander itself is clicked', async () => {
    renderWithProviders(<SessionsTable sessions={[session({ session_id: 'aaaaaaaa1111' })]} />);
    const toggle = screen.getByRole('button', {
      name: /expand details for session aaaaaaaa/i,
    });

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Token breakdown')).toBeInTheDocument();
    // aria-controls points at the detail cell the click revealed.
    const controlled = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
    expect(controlled).toHaveTextContent('Token breakdown');

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Token breakdown')).not.toBeInTheDocument();
  });
});

describe('SessionsTable sidecar column', () => {
  beforeEach(() => vi.clearAllMocks());

  const twoHosts = () =>
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [
        { sidecar_id: 'laptop', custom_name: 'My Laptop' },
        { sidecar_id: 'desktop', hostname: 'work-desktop' },
      ],
    } as never);

  it('shows a Sidecar column with per-row labels when >1 host feeds the fleet', async () => {
    twoHosts();
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({ session_id: 'aaaa1111', sidecar_id: 'laptop' }),
          session({ session_id: 'bbbb2222', sidecar_id: 'desktop' }),
        ]}
      />,
    );
    expect(await screen.findByRole('columnheader', { name: 'Sidecar' })).toBeInTheDocument();
    expect(screen.getByText('My Laptop')).toBeInTheDocument();
    expect(screen.getByText('work-desktop')).toBeInTheDocument();
  });

  it('falls back to a dash for a session with no sidecar_id', async () => {
    twoHosts();
    renderWithProviders(
      <SessionsTable sessions={[session({ session_id: 'cccc3333', sidecar_id: null })]} />,
    );
    await screen.findByRole('columnheader', { name: 'Sidecar' });
    const row = screen.getByText('cccc3333').closest('tr')!;
    expect(within(row).getByText('—')).toBeInTheDocument();
  });

  it('omits the Sidecar column on a single-host fleet', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: 'laptop', custom_name: 'My Laptop' }],
    } as never);
    renderWithProviders(
      <SessionsTable sessions={[session({ session_id: 'aaaa1111', sidecar_id: 'laptop' })]} />,
    );
    await screen.findByText('aaaa1111');
    expect(screen.queryByRole('columnheader', { name: 'Sidecar' })).not.toBeInTheDocument();
    expect(screen.queryByText('My Laptop')).not.toBeInTheDocument();
  });

  it('shows the Project column with a cwd-titled badge when sessions are attributed', () => {
    renderWithProviders(
      <SessionsTable
        sessions={[session({ session_id: 'aaaa1111', project: 'runway', cwd: '/home/u/runway' })]}
      />,
    );
    expect(screen.getByRole('columnheader', { name: 'Project' })).toBeInTheDocument();
    expect(screen.getByText('runway')).toHaveAttribute('title', '/home/u/runway');
  });

  it('omits the Project column when no session has a project', () => {
    renderWithProviders(
      <SessionsTable sessions={[session({ session_id: 'aaaa1111', project: null })]} />,
    );
    expect(screen.queryByRole('columnheader', { name: 'Project' })).not.toBeInTheDocument();
  });
});

describe('SessionsTable narrow layout', () => {
  beforeEach(() => vi.clearAllMocks());

  const twoHosts = () =>
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [
        { sidecar_id: 'laptop', custom_name: 'My Laptop' },
        { sidecar_id: 'desktop', hostname: 'work-desktop' },
      ],
    } as never);

  it('bounds the Models column and keeps the full model id reachable', () => {
    renderWithProviders(
      <SessionsTable
        sessions={[session({ session_id: 'aaaa1111', models: ['claude-sonnet-4-5-20250929'] })]}
      />,
    );
    // The header and the cell carry the same width cap: a long model id must
    // not widen the column (and with it the table) on a phone.
    const header = screen.getByRole('columnheader', { name: 'Models' });
    expect(header).toHaveClass('max-w-[10rem]');
    expect(header).toHaveClass('sm:max-w-none');

    const badge = screen.getByText('claude-sonnet-4-5-20250929');
    const cell = badge.closest('td')!;
    expect(cell).toHaveClass('max-w-[10rem]');
    expect(cell).toHaveClass('sm:max-w-none');
    // The badge can shrink, and the full id stays readable on hover.
    expect(badge).toHaveClass('min-w-0');
    expect(badge).toHaveAttribute('title', 'claude-sonnet-4-5-20250929');
  });

  it('drops the Project, Sidecar and Cost columns below their breakpoints', async () => {
    twoHosts();
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({
            session_id: 'aaaa1111',
            project: 'runway',
            cwd: '/home/u/runway',
            sidecar_id: 'laptop',
          }),
        ]}
      />,
    );
    await screen.findByRole('columnheader', { name: 'Sidecar' });

    const row = screen.getByText('aaaa1111').closest('tr')!;
    const headers = screen.getAllByRole('columnheader');
    const cells = within(row).getAllByRole('cell');

    // The <TH> and its matching <TD> must carry the same responsive classes,
    // or the header and its data end up in different columns.
    const columnClasses: Record<string, string[]> = {
      Project: ['hidden', 'sm:table-cell'],
      Sidecar: ['hidden', 'md:table-cell'],
      Cost: ['hidden', 'md:table-cell'],
    };
    for (const [label, classes] of Object.entries(columnClasses)) {
      const index = headers.findIndex((h) => h.textContent?.trim() === label);
      expect(index, `no header for ${label}`).toBeGreaterThanOrEqual(0);
      for (const cls of classes) {
        expect(headers[index], `${label} header`).toHaveClass(cls);
        expect(cells[index], `${label} cell`).toHaveClass(cls);
      }
    }

    // The columns that always show carry no hiding classes.
    for (const label of ['Session', 'Models', 'Duration', 'Messages', 'Tokens']) {
      const index = headers.findIndex((h) => h.textContent?.trim() === label);
      expect(index, `no header for ${label}`).toBeGreaterThanOrEqual(0);
      expect(headers[index], `${label} header`).not.toHaveClass('hidden');
      expect(cells[index], `${label} cell`).not.toHaveClass('hidden');
    }
  });

  it('spans the detail row across the widest layout so the panel fills the row', async () => {
    twoHosts();
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({
            session_id: 'aaaa1111',
            project: 'runway',
            cwd: '/home/u/runway',
            sidecar_id: 'laptop',
          }),
        ]}
      />,
    );
    await screen.findByRole('columnheader', { name: 'Sidecar' });
    await userEvent.click(screen.getByText('aaaa1111').closest('tr')!);

    // Six always-visible columns + Cost + the optional Project/Sidecar ones.
    // The colspan is the widest layout's count: an over-wide colspan is
    // clamped by the table model once the extra columns are hidden.
    const detail = screen.getByText('Token breakdown').closest('td')!;
    expect(detail).toHaveAttribute('colspan', '9');
  });
});

describe('SessionsTable sortable headers', () => {
  it('renders plain, non-interactive headers when no onSort is provided', () => {
    renderWithProviders(<SessionsTable sessions={[session({ session_id: 'aaaa1111' })]} />);
    const header = screen.getByRole('columnheader', { name: 'Tokens' });
    expect(within(header).queryByRole('button')).not.toBeInTheDocument();
  });

  it('renders header buttons and fires onSort with the clicked column key', async () => {
    const onSort = vi.fn();
    renderWithProviders(
      <SessionsTable
        sessions={[session({ session_id: 'aaaa1111' })]}
        sort={{ by: 'recent', dir: 'desc' }}
        onSort={onSort}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /Tokens/ }));
    expect(onSort).toHaveBeenCalledWith('tokens');
    await userEvent.click(screen.getByRole('button', { name: /Cost/ }));
    expect(onSort).toHaveBeenCalledWith('cost');
  });

  it('marks the active column with an aria-sort matching the direction', () => {
    renderWithProviders(
      <SessionsTable
        sessions={[session({ session_id: 'aaaa1111' })]}
        sort={{ by: 'duration', dir: 'asc' }}
        onSort={vi.fn()}
      />,
    );
    expect(screen.getByRole('columnheader', { name: /Duration/ })).toHaveAttribute(
      'aria-sort',
      'ascending',
    );
    expect(screen.getByRole('columnheader', { name: /Tokens/ })).toHaveAttribute(
      'aria-sort',
      'none',
    );
  });
});

describe('SessionsTable subagent turns and requests', () => {
  it('shows the request count next to the turns when the two differ', async () => {
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({
            session_id: 'aaaaaaaa1111',
            subagents: [
              {
                subagent_type: 'Explore',
                turns: 6,
                requests: 484,
                tool_calls: 5,
                tokens_total: 2000,
                cost_usd: 0.2,
              },
            ],
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText('aaaaaaaa').closest('tr')!);
    expect(screen.getByText('Turns')).toBeInTheDocument();
    expect(screen.getByText('Requests')).toBeInTheDocument();
    expect(screen.getByText('484')).toBeInTheDocument();
  });

  it('omits the request count when it matches the turn count', async () => {
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({
            session_id: 'aaaaaaaa1111',
            subagents: [
              {
                subagent_type: 'Explore',
                turns: 3,
                requests: 3,
                tool_calls: 5,
                tokens_total: 2000,
                cost_usd: 0.2,
              },
            ],
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText('aaaaaaaa').closest('tr')!);
    expect(screen.getByText('Turns')).toBeInTheDocument();
    expect(screen.queryByText('Requests')).not.toBeInTheDocument();
  });

  it('counts a missing turn count as zero when comparing it with the requests', async () => {
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({
            session_id: 'aaaaaaaa1111',
            subagents: [
              {
                subagent_type: 'Explore',
                requests: 3,
                tool_calls: 0,
                tokens_total: 0,
              },
            ],
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText('aaaaaaaa').closest('tr')!);
    expect(screen.getByText('Turns')).toBeInTheDocument();
    expect(screen.getByText('Requests')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});
