import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './Tabs';

function Harness() {
  return (
    <Tabs defaultValue="overview">
      <TabsList>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="events">Events</TabsTrigger>
      </TabsList>
      <TabsContent value="overview">Overview panel</TabsContent>
      <TabsContent value="events">Events panel</TabsContent>
    </Tabs>
  );
}

describe('Tabs', () => {
  it('renders the triggers and the default panel', () => {
    render(<Harness />);
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Events' })).toBeInTheDocument();
    expect(screen.getByText('Overview panel')).toBeInTheDocument();
  });

  it('marks the default tab active', () => {
    render(<Harness />);
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute(
      'data-state',
      'active',
    );
  });

  it('switches panels when another tab is clicked', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('tab', { name: 'Events' }));
    expect(screen.getByText('Events panel')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Events' })).toHaveAttribute(
      'data-state',
      'active',
    );
  });
});
