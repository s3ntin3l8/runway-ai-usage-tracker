import { render, screen } from '@testing-library/react';
import { PageHeader } from './PageHeader';

describe('PageHeader', () => {
  it('renders the title as a heading', () => {
    render(<PageHeader title="Dashboard" />);
    expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
  });

  it('renders the description when provided', () => {
    render(<PageHeader title="Home" description="An overview" />);
    expect(screen.getByText('An overview')).toBeInTheDocument();
  });

  it('omits the description paragraph when not provided', () => {
    const { container } = render(<PageHeader title="Home" />);
    expect(container.querySelector('p')).toBeNull();
  });

  it('renders leading and actions slots', () => {
    render(
      <PageHeader
        title="Home"
        leading={<span>lead</span>}
        actions={<button>act</button>}
      />,
    );
    expect(screen.getByText('lead')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'act' })).toBeInTheDocument();
  });

  it('merges a custom className onto the header', () => {
    const { container } = render(<PageHeader title="Home" className="custom-x" />);
    expect(container.querySelector('header')).toHaveClass('custom-x');
  });
});
