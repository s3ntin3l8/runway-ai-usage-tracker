import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Tooltip, TooltipProvider } from './Tooltip';

describe('Tooltip', () => {
  it('renders the trigger child', () => {
    render(
      <TooltipProvider>
        <Tooltip content="Helpful hint">
          <button>Hover me</button>
        </Tooltip>
      </TooltipProvider>,
    );
    expect(screen.getByRole('button', { name: 'Hover me' })).toBeInTheDocument();
  });

  it('reveals the content on hover', async () => {
    render(
      <TooltipProvider delayDuration={0}>
        <Tooltip content="Helpful hint">
          <button>Hover me</button>
        </Tooltip>
      </TooltipProvider>,
    );
    await userEvent.hover(screen.getByRole('button', { name: 'Hover me' }));
    // Radix may render the content in multiple nodes (visible + a11y); use findAll.
    const tips = await screen.findAllByText('Helpful hint');
    expect(tips.length).toBeGreaterThan(0);
  });
});
