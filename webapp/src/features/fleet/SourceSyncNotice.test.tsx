import { render, screen } from '@testing-library/react';
import type { Sidecar } from '@/api/types';
import { SourceSyncNotice } from './SourceSyncNotice';

const DEVICE = `device-${'a'.repeat(32)}`;
const sidecar = (state: Sidecar['source_sync_state']): Sidecar => ({
  sidecar_id: DEVICE,
  source_sync_state: state,
});

describe('SourceSyncNotice', () => {
  it('warns about an online device that never synced, with a link to the proxy docs', () => {
    render(<SourceSyncNotice sidecar={sidecar('never')} online />);
    expect(screen.getByText(/has not synced its sources/i)).toBeInTheDocument();
    expect(screen.getByText(/POST \/api\/v1\/fleet\/source-sync/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /verify the bypass/i })).toHaveAttribute(
      'href',
      expect.stringContaining('docs/forward-auth.md#verify-the-bypass'),
    );
  });

  it.each([['ok'], ['pending'], [null], [undefined]] as const)(
    'stays hidden when the state is %s',
    (state) => {
      const { container } = render(<SourceSyncNotice sidecar={sidecar(state)} online />);
      expect(container).toBeEmptyDOMElement();
    },
  );

  it('stays hidden for a device that is offline', () => {
    const { container } = render(<SourceSyncNotice sidecar={sidecar('never')} online={false} />);
    expect(container).toBeEmptyDOMElement();
  });
});
