import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { EventsTab } from './EventsTab';
import * as api from '@/api/endpoints';
import { currentPeriod, emptyEvents, eventsResponse, pastPeriod } from './test-fixtures';

vi.mock('@/api/endpoints');

describe('EventsTab', () => {
  beforeEach(() => vi.clearAllMocks());

  it('does not fetch while inactive', () => {
    renderWithProviders(
      <EventsTab
        providerId="anthropic"
        accountId="me@example.com"
        period={currentPeriod()}
        active={false}
      />,
    );
    expect(api.fetchEvents).not.toHaveBeenCalled();
  });

  it('shows the empty state when no events this month', async () => {
    vi.mocked(api.fetchEvents).mockResolvedValue(emptyEvents());
    renderWithProviders(
      <EventsTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    expect(await screen.findByText(/no events in/i)).toBeInTheDocument();
  });

  it('renders a page of events and the range counter', async () => {
    vi.mocked(api.fetchEvents).mockResolvedValue(eventsResponse(25, 60));
    renderWithProviders(
      <EventsTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    expect(await screen.findByText(/^Events ·/)).toBeInTheDocument();
    expect(await screen.findByText(/showing 1–25 of 60/i)).toBeInTheDocument();
    // 25 data rows rendered.
    expect(screen.getAllByRole('row').length).toBeGreaterThan(25);
  });

  it('pages forward and back', async () => {
    vi.mocked(api.fetchEvents).mockResolvedValue(eventsResponse(25, 60));
    renderWithProviders(
      <EventsTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    const next = await screen.findByRole('button', { name: /next page/i });
    expect(screen.getByRole('button', { name: /previous page/i })).toBeDisabled();

    await userEvent.click(next);
    await waitFor(() =>
      expect(api.fetchEvents).toHaveBeenCalledWith(expect.objectContaining({ offset: 25 })),
    );
  });

  it('scopes the query to the selected month with since and until', async () => {
    vi.mocked(api.fetchEvents).mockResolvedValue(emptyEvents());
    const period = pastPeriod('2026-01');
    renderWithProviders(
      <EventsTab providerId="anthropic" accountId="me@example.com" period={period} active />,
    );
    await waitFor(() =>
      expect(api.fetchEvents).toHaveBeenCalledWith(
        expect.objectContaining({ since: period.range.since, until: period.range.until }),
      ),
    );
  });

  it('renders an error badge for error events', async () => {
    vi.mocked(api.fetchEvents).mockResolvedValue({
      events: [{ event_id: 'x', kind: 'error', ts: new Date().toISOString() }],
      total: 1,
      limit: 25,
      offset: 0,
    });
    renderWithProviders(
      <EventsTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} active />,
    );
    expect(await screen.findByText('error')).toBeInTheDocument();
  });
});
