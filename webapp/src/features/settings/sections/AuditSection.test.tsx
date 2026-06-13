import { screen } from '@testing-library/react';
import type { AuditEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { AuditSection } from './AuditSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');

const entry = (o: Partial<AuditEntry> = {}): AuditEntry => ({
  id: 1,
  ts: '2026-06-13T10:00:00Z',
  actor: 'admin',
  action: 'sidecar.pause',
  target_id: 'laptop',
  source_ip: '10.0.0.1',
  ...o,
});

describe('AuditSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the empty state when there are no entries', async () => {
    vi.mocked(api.fetchAuditLog).mockResolvedValue({ entries: [] });
    renderWithProviders(<AuditSection />);
    expect(await screen.findByText(/no admin mutations recorded/i)).toBeInTheDocument();
  });

  it('renders the error state when the query fails', async () => {
    vi.mocked(api.fetchAuditLog).mockRejectedValue(new Error('forbidden'));
    renderWithProviders(<AuditSection />);
    expect(await screen.findByText(/audit log unavailable/i)).toBeInTheDocument();
    expect(screen.getByText('forbidden')).toBeInTheDocument();
  });

  it('renders a table of audit entries and requests 200 rows', async () => {
    vi.mocked(api.fetchAuditLog).mockResolvedValue({
      entries: [entry({ action: 'config.update', actor: 'me', target_id: 'claude' })],
    });
    renderWithProviders(<AuditSection />);

    expect(await screen.findByText('config.update')).toBeInTheDocument();
    expect(screen.getByText('me')).toBeInTheDocument();
    expect(screen.getByText('claude')).toBeInTheDocument();
    expect(api.fetchAuditLog).toHaveBeenCalledWith(200);
  });
});
