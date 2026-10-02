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

  it('allows a page to explicitly opt into the shared sticky header behavior', () => {
    const { container } = render(<PageHeader title="Provider" sticky />);
    expect(container.querySelector('header')).toHaveClass('sticky', 'top-0', 'z-20');
  });

  it('can opt out of sticky positioning when embedded in a scroll region', () => {
    const { container } = render(<PageHeader title="Dialog" sticky={false} />);
    expect(container.querySelector('header')).not.toHaveClass('sticky');
  });

  it('supports a custom responsive layout inside the shared header', () => {
    const { container } = render(
      <PageHeader title="Provider" sticky>
        <div>Responsive provider controls</div>
      </PageHeader>,
    );
    expect(container.querySelector('header')).toHaveClass('sticky');
    expect(screen.getByText('Responsive provider controls')).toBeInTheDocument();
  });
});
