import { act, screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { OfflineBanner } from './OfflineBanner';

describe('OfflineBanner', () => {
  it('shows only while offline and hides on reconnect', () => {
    const { container } = renderWithProviders(<OfflineBanner />);
    expect(container).toBeEmptyDOMElement();

    act(() => window.dispatchEvent(new Event('offline')));
    expect(screen.getByText(/showing the last loaded data/i)).toBeInTheDocument();

    act(() => window.dispatchEvent(new Event('online')));
    expect(container).toBeEmptyDOMElement();
  });
});
