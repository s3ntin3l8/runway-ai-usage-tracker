import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfirmDialog } from './ConfirmDialog';

const open = (props: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) => {
  const onConfirm = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <ConfirmDialog
      open
      onOpenChange={onOpenChange}
      title="Remove it?"
      description="This cannot be undone."
      onConfirm={onConfirm}
      {...props}
    />,
  );
  return { onConfirm, onOpenChange };
};

describe('ConfirmDialog', () => {
  it('shows the title and description', () => {
    open();
    expect(screen.getByRole('dialog', { name: 'Remove it?' })).toBeInTheDocument();
    expect(screen.getByText('This cannot be undone.')).toBeInTheDocument();
  });

  it('confirms with a custom label', async () => {
    const { onConfirm } = open({ confirmLabel: 'Delete rule' });
    await userEvent.click(screen.getByRole('button', { name: 'Delete rule' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('cancels without confirming', async () => {
    const { onConfirm, onOpenChange } = open();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('disables both actions while the request is pending', () => {
    open({ pending: true });
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove' })).toBeDisabled();
  });
});
