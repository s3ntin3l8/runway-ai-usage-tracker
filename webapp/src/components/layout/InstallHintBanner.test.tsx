import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { InstallHintBanner } from './InstallHintBanner';
import * as install from '@/hooks/useInstallPrompt';

vi.mock('@/hooks/useInstallPrompt', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useInstallPrompt')>()),
  useInstallPrompt: vi.fn(),
}));

const base = { canInstall: false, promptInstall: vi.fn(), isStandalone: false };

describe('InstallHintBanner', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it('shows the iOS Add-to-Home-Screen hint and dismisses persistently', async () => {
    vi.mocked(install.useInstallPrompt).mockReturnValue({ ...base, isIOS: true });
    render(<InstallHintBanner />);

    expect(screen.getByText(/Add to Home Screen/i)).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/dismiss install hint/i));

    expect(screen.queryByText(/Add to Home Screen/i)).not.toBeInTheDocument();
    expect(localStorage.getItem('runway:ios-install-hint-dismissed')).toBe('1');
  });

  it('renders nothing on non-iOS browsers', () => {
    vi.mocked(install.useInstallPrompt).mockReturnValue({ ...base, isIOS: false });
    const { container } = render(<InstallHintBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});
