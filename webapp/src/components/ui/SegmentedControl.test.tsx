import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SegmentedControl } from './SegmentedControl';

const OPTIONS = [
  { value: 'tokens', label: 'Tokens' },
  { value: 'cost', label: 'Cost' },
] as const;

describe('SegmentedControl', () => {
  it('is a named group of radio buttons, not a tab list', () => {
    render(
      <SegmentedControl
        value="tokens"
        onChange={() => {}}
        label="Chart metric"
        options={OPTIONS}
      />,
    );
    const group = screen.getByRole('group', { name: 'Chart metric' });
    expect(within(group).getByRole('radio', { name: 'Tokens' })).toBeChecked();
    expect(within(group).getByRole('radio', { name: 'Cost' })).not.toBeChecked();
    // A tab trigger points at a tabpanel. These controls switch what one
    // panel shows, so there is no panel to point at — the tab semantics are
    // what left aria-controls dangling on a missing element.
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
  });

  it('reports the chosen option', async () => {
    const onChange = vi.fn();
    render(
      <SegmentedControl
        value="tokens"
        onChange={onChange}
        label="Chart metric"
        options={OPTIONS}
      />,
    );
    await userEvent.click(screen.getByRole('radio', { name: 'Cost' }));
    expect(onChange).toHaveBeenCalledWith('cost');
  });
});
