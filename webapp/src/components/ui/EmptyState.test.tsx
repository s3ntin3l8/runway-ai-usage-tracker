import { render, screen } from '@testing-library/react';
import { Inbox } from 'lucide-react';
import { EmptyState } from './EmptyState';

describe('EmptyState', () => {
  it('renders the title only', () => {
    render(<EmptyState title="Nothing here" />);
    expect(screen.getByText('Nothing here')).toBeInTheDocument();
  });

  it('renders description and action when provided', () => {
    render(
      <EmptyState
        title="Empty"
        description="Add your first item"
        action={<button>Add</button>}
      />,
    );
    expect(screen.getByText('Add your first item')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument();
  });

  it('renders an icon when provided', () => {
    const { container } = render(<EmptyState icon={Inbox} title="With icon" />);
    expect(container.querySelector('svg')).toBeInTheDocument();
  });

  it('omits description and action when not provided', () => {
    const { container } = render(<EmptyState title="Bare" />);
    // only the title <p> exists, no extra description/action wrappers
    expect(container.querySelectorAll('p')).toHaveLength(1);
    expect(container.querySelector('svg')).toBeNull();
  });
});
