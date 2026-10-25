import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Tooltip, TooltipProvider } from './Tooltip';
import { Button } from './Button';

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

  it('does not add a second tab stop to an already focusable child', () => {
    render(
      <TooltipProvider>
        <Tooltip content="Helpful hint">
          <button>Press me</button>
        </Tooltip>
      </TooltipProvider>,
    );
    const button = screen.getByRole('button', { name: 'Press me' });
    // The trigger IS the button. A wrapper span with a tab stop around it
    // would make this one control two tab stops.
    expect(button.getAttribute('tabindex')).toBeNull();
    expect(button.closest('[tabindex]')).toBeNull();
  });

  it('gives a role-less child a focusable host', () => {
    render(
      <TooltipProvider>
        <Tooltip content="Helpful hint">
          <span>Plain text</span>
        </Tooltip>
      </TooltipProvider>,
    );
    // The hint used to be hover-only: nothing in it could take focus.
    const host = screen.getByText('Plain text').closest('[tabindex]');
    expect(host).toHaveAttribute('tabindex', '0');
  });

  it('treats the app Button as already focusable', () => {
    render(
      <TooltipProvider>
        <Tooltip content="Helpful hint">
          <Button variant="ghost" size="sm">
            Press me
          </Button>
        </Tooltip>
      </TooltipProvider>,
    );
    const button = screen.getByRole('button', { name: 'Press me' });
    expect(button.getAttribute('tabindex')).toBeNull();
    expect(button.closest('[tabindex]')).toBeNull();
  });
});
