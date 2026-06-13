import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { ThemeProvider } from '@/hooks/useTheme';
import { KitPage } from './KitPage';

// Stub the chart bundle (no canvas in jsdom). The SampleChart option-builder
// still runs through useChartTokens, which needs the real ThemeProvider below.
vi.mock('@/components/charts/EChart', () => ({
  EChart: () => <div data-testid="echart" />,
}));

describe('KitPage', () => {
  it('renders the design-system gallery sections without throwing', () => {
    renderWithProviders(
      <ThemeProvider>
        <KitPage />
      </ThemeProvider>,
    );
    expect(screen.getByText('UI Kit')).toBeInTheDocument();
    expect(screen.getByText('Buttons')).toBeInTheDocument();
    expect(screen.getByText('Badges & status')).toBeInTheDocument();
    expect(screen.getByText('Gauges')).toBeInTheDocument();
    expect(screen.getByText('Form controls')).toBeInTheDocument();
    expect(screen.getByText('Chart theme bridge')).toBeInTheDocument();
  });

  it('shows the showcased primitive content', () => {
    renderWithProviders(
      <ThemeProvider>
        <KitPage />
      </ThemeProvider>,
    );
    expect(screen.getByRole('button', { name: 'Primary' })).toBeInTheDocument();
    expect(screen.getByText('accent')).toBeInTheDocument();
    expect(screen.getByText('claude-sonnet-4-6')).toBeInTheDocument();
    expect(screen.getByTestId('echart')).toBeInTheDocument();
  });
});
