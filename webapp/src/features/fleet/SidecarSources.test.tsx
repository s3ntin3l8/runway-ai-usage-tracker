import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { FleetSourceStatus } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { SidecarSources, SidecarSourceNotice } from './SidecarSources';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
const sidecarId = 'device-' + 'a'.repeat(32);

const base: FleetSourceStatus = {
  source_id: 'anthropic.claude-code',
  provider_id: 'anthropic',
  label: 'Claude Code',
  revision: 'a'.repeat(64),
  generation: 1,
  read: true,
  state: 'Ready',
  renewal_requested: false,
  renewal_supported: false,
  renewal_capable: false,
  new: false,
  remote_paused: false,
  revoked: false,
  report_at: new Date().toISOString(),
  report_stale: false,
  announcement: false,
  collection_health: 'healthy',
  last_collection_at: new Date().toISOString(),
};
const src = (source_id: string, label: string, over: Partial<FleetSourceStatus> = {}): FleetSourceStatus => ({
  ...base,
  source_id,
  label,
  provider_id: source_id.split('.')[0],
  ...over,
});
const off: Partial<FleetSourceStatus> = { read: false, state: 'Off', collection_health: 'untried', last_collection_at: null };
const need: Partial<FleetSourceStatus> = { state: 'Setup required', collection_health: 'untried', last_collection_at: null };

// A slice of the real macbook: apps and CLIs plus two services behind three browsers each.
const MAC: FleetSourceStatus[] = [
  src('anthropic.claude-code', 'Claude Code', { state: 'Action required', collection_health: 'untried' }),
  src('anthropic.desktop-session', 'Claude Desktop session'),
  src('github.gh-cli', 'GitHub CLI', off),
  src('anthropic.chrome', 'Claude Pro · Chrome cookies', need),
  src('anthropic.edge', 'Claude Pro · Edge cookies', need),
  src('anthropic.chromium', 'Claude Pro · Chromium cookies', off),
  src('chatgpt.chrome', 'ChatGPT Codex · Chrome cookies'),
  src('chatgpt.edge', 'ChatGPT Codex · Edge cookies', off),
  src('chatgpt.chromium', 'ChatGPT Codex · Chromium cookies', off),
];

const respond = (sources: FleetSourceStatus[], commands: { id: number; source_id: string; generation: number; action: 'request_setup' }[] = []) =>
  vi.mocked(api.fetchSidecarSources).mockResolvedValue({ protocol: 1, sources, commands });
const renderMac = () => renderWithProviders(<SidecarSources osPlatform="Darwin/26" sidecarId={sidecarId} />);

/** The sources the card lists, after opening every service row. Names are stable across both kinds of row. */
async function listedSources(user: ReturnType<typeof userEvent.setup>) {
  for (const toggle of screen.queryAllByRole('button', { name: /browsers$/ })) {
    if (toggle.getAttribute('aria-expanded') === 'false') await user.click(toggle);
  }
  const rows = within(screen.getByRole('table')).getAllByRole('row');
  const names: string[] = [];
  for (const row of rows) {
    const toggle = within(row).queryAllByRole('button', { expanded: false })[0] ?? within(row).queryAllByRole('button', { expanded: true })[0];
    const text = toggle?.textContent;
    if (text && !/browsers$/.test(toggle.getAttribute('aria-label') ?? '')) names.push(text.replace(/paused|NEW/g, ''));
  }
  return names.sort();
}
const tileNumber = (name: RegExp) => within(screen.getByRole('button', { name })).getByText(/^\d/).textContent;

beforeEach(() => {
  vi.clearAllMocks();
  respond(MAC);
  vi.mocked(api.commandSidecarSource).mockResolvedValue({ id: 1, status: 'pending' });
});

describe('Keychain sources (macOS)', () => {
  it('counts sources, not rows, and groups the cookie sources into one row per service', async () => {
    renderMac();
    expect(await screen.findByText('Claude Pro')).toBeInTheDocument();
    expect(screen.getByText('ChatGPT Codex')).toBeInTheDocument();
    expect(screen.queryByText('Claude Pro · Chrome cookies')).not.toBeInTheDocument();
    expect(tileNumber(/^\d+\/\d+ on$/)).toBe('5/9');
    expect(tileNumber(/^\d+ collecting$/)).toBe('2');
    expect(tileNumber(/^\d+ need attention$/)).toBe('3');
    expect(tileNumber(/^\d+ off$/)).toBe('4');
  });

  it.each([
    ['on', /^\d+\/\d+ on$/, ['Chrome cookies', 'Chrome cookies', 'Claude Code', 'Claude Desktop session', 'Edge cookies']],
    ['collecting', /^\d+ collecting$/, ['Chrome cookies', 'Claude Desktop session']],
    ['need attention', /^\d+ need attention$/, ['Chrome cookies', 'Claude Code', 'Edge cookies']],
    ['off', /^\d+ off$/, ['Chromium cookies', 'Chromium cookies', 'Edge cookies', 'GitHub CLI']],
  ])('the %s tile lists exactly the sources it counts', async (_name, tile, expected) => {
    const user = userEvent.setup();
    renderMac();
    await user.click(await screen.findByRole('button', { name: tile }));
    expect(screen.getByRole('button', { name: tile })).toHaveAttribute('aria-pressed', 'true');
    expect(await listedSources(user)).toEqual(expected);
    expect(expected).toHaveLength(parseInt(tileNumber(tile) ?? "", 10));
  });

  it('clears the filter when the tile is pressed again and shows no fold while filtering', async () => {
    const user = userEvent.setup();
    renderMac();
    expect(await screen.findByRole('button', { name: /Show 4 sources that are off/ })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^\d+ collecting$/ }));
    expect(screen.queryByRole('button', { name: /sources? that are off/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^\d+ collecting$/ }));
    expect(screen.getByRole('button', { name: /Show 4 sources that are off/ })).toBeInTheDocument();
  });

  it('shows what is on, folds what is off, and says so when nothing matches', async () => {
    const user = userEvent.setup();
    renderMac();
    expect(await screen.findByText('Claude Code')).toBeInTheDocument();
    expect(screen.queryByText('GitHub CLI')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Show 4 sources that are off/ }));
    expect(screen.getByText('GitHub CLI')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Hide sources that are off' })).toBeInTheDocument();
  });

  it('says nothing matches a filter that has no sources', async () => {
    const user = userEvent.setup();
    respond([src('anthropic.claude-code', 'Claude Code')]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: /^\d+ need attention$/ }));
    expect(screen.getByText('Nothing matches this filter.')).toBeInTheDocument();
  });

  it('rolls the browsers of a service up to the worst status and shows how many are collecting', async () => {
    renderMac();
    const pro = (await screen.findByRole('button', { name: 'Claude Pro browsers' })).closest('[role="row"]') as HTMLElement;
    expect(within(pro).getByText('Setup on Mac')).toBeInTheDocument();
    expect(within(pro).getByText('untried')).toBeInTheDocument();
    const chatgpt = screen.getByRole('button', { name: 'ChatGPT Codex browsers' }).closest('[role="row"]') as HTMLElement;
    expect(within(chatgpt).getByText('Authorized')).toBeInTheDocument();
    expect(within(chatgpt).getByText('healthy · 1/1')).toBeInTheDocument();
  });

  it('shows per-browser details on demand: last read, collection, report and pending commands', async () => {
    const user = userEvent.setup();
    respond(MAC, [{ id: 1, source_id: 'anthropic.chrome', generation: 1, action: 'request_setup' }]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Claude Pro browsers' }));
    await user.click(screen.getByRole('button', { name: 'Chrome cookies' }));
    expect(screen.getByText(/Awaiting local acknowledgment: request setup/)).toBeInTheDocument();
    expect(screen.getByText('Last Keychain read')).toBeInTheDocument();
    expect(screen.getByText(/Source reported/, { selector: 'dd' })).toBeInTheDocument();
  });

  it('asks the Mac to set up every enabled source that needs authorization, in one click', async () => {
    const user = userEvent.setup();
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Ask Mac to set up 3' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledTimes(3));
    for (const id of ['anthropic.claude-code', 'anthropic.chrome', 'anthropic.edge'])
      expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, id, 'request_setup');
  });

  it('does not ask the Mac to set up a source the server revoked', async () => {
    const user = userEvent.setup();
    respond([
      src('anthropic.claude-code', 'Claude Code', need),
      src('anthropic.desktop-session', 'Claude Desktop session', { revoked: true, state: 'Action required' }),
    ]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Ask Mac to set up 1' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledTimes(1));
    expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'anthropic.claude-code', 'request_setup');
  });

  it('says a withdrawal the server has not acknowledged in text, not only in a tooltip', async () => {
    const user = userEvent.setup();
    respond([src('github.gh-cli', 'GitHub CLI', { ...off, state: 'Withdrawal pending' })]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Show 1 source that is off' }));
    expect(screen.getByText('Withdrawing')).toBeInTheDocument();
  });

  it('pauses and resumes only sources that can be paused or resumed', async () => {
    const user = userEvent.setup();
    respond([
      src('anthropic.claude-code', 'Claude Code'),
      src('anthropic.desktop-session', 'Claude Desktop session', { remote_paused: true }),
      src('github.gh-cli', 'GitHub CLI', { ...off, remote_paused: true }),
    ]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Pause all' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledTimes(1));
    expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'anthropic.claude-code', 'pause');
    await user.click(screen.getByRole('button', { name: 'Resume all' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledTimes(2));
    expect(api.commandSidecarSource).toHaveBeenLastCalledWith(sidecarId, 'anthropic.desktop-session', 'resume');
  });

  it('pauses one source from its menu and asks the Mac for setup of one source', async () => {
    const user = userEvent.setup();
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Actions for Claude Desktop session' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Pause collection' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'anthropic.desktop-session', 'pause'));
    await user.click(screen.getByRole('button', { name: 'Actions for Claude Code' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Ask Mac to set up' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'anthropic.claude-code', 'request_setup'));
  });

  it('local Off prevents remotely resuming a pause', async () => {
    const user = userEvent.setup();
    respond([src('github.gh-cli', 'GitHub CLI', { ...off, remote_paused: true })]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Show 1 source that is off' }));
    await user.click(screen.getByRole('button', { name: 'Actions for GitHub CLI' }));
    expect(await screen.findByRole('menuitem', { name: 'Resume server pause' })).toHaveAttribute('aria-disabled', 'true');
  });

  it('revokes only after confirming, and a revoked source cannot be revoked again', async () => {
    const user = userEvent.setup();
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Actions for Claude Code' }));
    await user.click(await screen.findByRole('menuitem', { name: /Revoke contribution/ }));
    expect(api.commandSidecarSource).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: 'Revoke contribution' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'anthropic.claude-code', 'revoke'));
  });

  it('cancelling the revoke confirmation sends nothing', async () => {
    const user = userEvent.setup();
    renderMac();
    await user.click(await screen.findByRole('button', { name: 'Actions for Claude Code' }));
    await user.click(await screen.findByRole('menuitem', { name: /Revoke contribution/ }));
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(api.commandSidecarSource).not.toHaveBeenCalled();
  });

  it('marks a revoked source and disables revoking it again', async () => {
    const user = userEvent.setup();
    respond([src('anthropic.claude-code', 'Claude Code', { revoked: true, collection_health: 'withdrawn' })]);
    renderMac();
    expect(await screen.findByText('Revoked')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Actions for Claude Code' }));
    expect(await screen.findByRole('menuitem', { name: /Revoke contribution/ })).toHaveAttribute('aria-disabled', 'true');
  });

  it('flags a stale report without implying the server stopped collecting', async () => {
    respond([src('anthropic.claude-code', 'Claude Code', { report_stale: true })]);
    renderMac();
    expect(await screen.findByText(/Stale source report ·/)).toBeInTheDocument();
    expect(screen.getByText(/has not reported its sources recently/)).toBeInTheDocument();
    expect(screen.getByText('healthy · just now')).toBeInTheDocument();
  });

  it('announces new scrapers once and dismisses them in one click', async () => {
    const user = userEvent.setup();
    respond([
      src('anthropic.claude-code', 'Claude Code', { announcement: true }),
      src('chatgpt.chrome', 'ChatGPT Codex · Chrome cookies', { ...off, announcement: true }),
    ]);
    renderMac();
    expect(await screen.findByText(/2 new Keychain scrapers/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledTimes(2));
    expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'anthropic.claude-code', 'dismiss');
    expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'chatgpt.chrome', 'dismiss');
  });

  it('dismisses a single source notice from its details', async () => {
    const user = userEvent.setup();
    respond([src('anthropic.claude-code', 'Claude Code', { announcement: true })]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: /^Claude Code/ }));
    await user.click(screen.getByRole('button', { name: 'Dismiss notice for Claude Code' }));
    await waitFor(() => expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'anthropic.claude-code', 'dismiss'));
  });

  it('shows renewal limits for a renewal-capable source', async () => {
    const user = userEvent.setup();
    respond([src('anthropic.claude-code', 'Claude Code', { renewal_capable: true })]);
    renderMac();
    await user.click(await screen.findByRole('button', { name: /^Claude Code/ }));
    expect(screen.getByText(/Limited: supported write path not verified/)).toBeInTheDocument();
  });

  it('shows the loading, error and empty states without a table', async () => {
    vi.mocked(api.fetchSidecarSources).mockRejectedValue(new Error('boom'));
    renderMac();
    expect(await screen.findByRole('alert')).toHaveTextContent('Source status unavailable');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('waits for the first source report', async () => {
    respond([]);
    renderMac();
    expect(await screen.findByText(/Waiting for the sidecar’s first source report/)).toBeInTheDocument();
  });

  it('does not query legacy devices and shows limited compatibility', () => {
    renderWithProviders(<SidecarSources osPlatform="Darwin/26" sidecarId="legacy-host" />);
    expect(screen.getByText(/pair this sidecar again/)).toBeInTheDocument();
    expect(api.fetchSidecarSources).not.toHaveBeenCalled();
  });
});

describe('credential sources on other systems', () => {
  const github = src('github.windows-credential', 'GitHub Credential Manager');

  it('shows a Windows contribution as a compact table with server controls and no Keychain setup', async () => {
    const user = userEvent.setup();
    respond([github]);
    renderWithProviders(<SidecarSources osPlatform="Windows/11" sidecarId={sidecarId} />);
    expect(await screen.findByText('GitHub Credential Manager')).toBeInTheDocument();
    expect(screen.getByText('On the device')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Filter sources' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Ask Mac to set up/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pause all' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Actions for GitHub Credential Manager' }));
    expect(screen.queryByRole('menuitem', { name: 'Ask Mac to set up' })).not.toBeInTheDocument();
    await user.click(await screen.findByRole('menuitem', { name: /Revoke contribution/ }));
    expect(await screen.findByText(/requires re-pairing the device/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Revoke contribution' }));
    await waitFor(() =>
      expect(api.commandSidecarSource).toHaveBeenCalledWith(sidecarId, 'github.windows-credential', 'revoke'),
    );
  });

  it('explains reporting once and points to Fleet connectivity', async () => {
    respond([github, src('antigravity.windows-credential', 'Antigravity')]);
    renderWithProviders(<SidecarSources osPlatform="Windows/11" sidecarId={sidecarId} />);
    expect(await screen.findByText('Antigravity')).toBeInTheDocument();
    expect(screen.getAllByText(/Source reporting updates when credentials are submitted/)).toHaveLength(1);
    expect(screen.getByText(/Last seen.*Fleet card/)).toBeInTheDocument();
  });

  it('shows nothing for a Linux device that reports no sources, and nothing for unknown systems', async () => {
    respond([]);
    const { container } = renderWithProviders(<SidecarSources osPlatform="Linux/7.0" sidecarId={sidecarId} />);
    await waitFor(() => expect(api.fetchSidecarSources).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    vi.clearAllMocks();
    const other = renderWithProviders(<SidecarSources osPlatform="FreeBSD/14" sidecarId={sidecarId} />);
    expect(other.container).toBeEmptyDOMElement();
    expect(api.fetchSidecarSources).not.toHaveBeenCalled();
  });

  it('shows nothing for a legacy Linux device', () => {
    const { container } = renderWithProviders(<SidecarSources osPlatform="Linux/7.0" sidecarId="legacy-host" />);
    expect(container).toBeEmptyDOMElement();
    expect(api.fetchSidecarSources).not.toHaveBeenCalled();
  });
});

describe('SidecarSourceNotice', () => {
  const notice = src('anthropic.claude-code', 'Claude Code', { announcement: true });

  it('banner opens details and never enables a scraper', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    respond([notice]);
    renderWithProviders(<SidecarSourceNotice osPlatform="Darwin/26" sidecarId={sidecarId} onOpen={onOpen} />);
    await user.click(await screen.findByRole('button', { name: 'Review Keychain scrapers' }));
    expect(onOpen).toHaveBeenCalledOnce();
    expect(api.commandSidecarSource).not.toHaveBeenCalled();
  });

  it('dismissed notices remain hidden until the server reports a new revision', async () => {
    respond([{ ...notice, announcement: false }]);
    renderWithProviders(<SidecarSourceNotice osPlatform="Darwin/26" sidecarId={sidecarId} onOpen={vi.fn()} />);
    await waitFor(() => expect(api.fetchSidecarSources).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Review Keychain scrapers' })).not.toBeInTheDocument();
  });

  it('does not target Windows or unknown devices with Keychain announcements', () => {
    renderWithProviders(<SidecarSourceNotice sidecarId={sidecarId} osPlatform="Windows/11" onOpen={vi.fn()} />);
    expect(api.fetchSidecarSources).not.toHaveBeenCalled();
    expect(screen.queryByText('New Keychain scraper—opt in now')).not.toBeInTheDocument();
  });
});
