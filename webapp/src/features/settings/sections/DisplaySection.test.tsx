import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { ThemeProvider } from '@/hooks/useTheme';
import { DisplaySection } from './DisplaySection';

describe('DisplaySection', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it('renders the theme selector reflecting the stored preference', () => {
    localStorage.setItem('runway_theme', 'light');
    renderWithProviders(
      <ThemeProvider>
        <DisplaySection />
      </ThemeProvider>,
    );
    expect(screen.getByText('Appearance')).toBeInTheDocument();
    // Trigger shows the current value's display label.
    expect(screen.getByRole('combobox')).toHaveTextContent(/light/i);
  });

  it('persists the chosen theme to localStorage', async () => {
    renderWithProviders(
      <ThemeProvider>
        <DisplaySection />
      </ThemeProvider>,
    );
    await userEvent.click(screen.getByRole('combobox'));
    await userEvent.click(await screen.findByRole('option', { name: /dark/i }));
    expect(localStorage.getItem('runway_theme')).toBe('dark');
  });
});
