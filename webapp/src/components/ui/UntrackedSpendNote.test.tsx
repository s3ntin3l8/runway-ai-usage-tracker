import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { UntrackedSpendNote } from './UntrackedSpendNote';

it('renders the line when untracked spend is set', () => {
  renderWithProviders(<UntrackedSpendNote card={{ untracked_spend_usd: 0.8, currency: 'USD' }} />);
  expect(screen.getByText("$0.80 of this period's spend is not from tracked clients")).toBeInTheDocument();
});

it('renders nothing when untracked spend is null or absent', () => {
  const { container, rerender } = renderWithProviders(<UntrackedSpendNote card={{ untracked_spend_usd: null }} />);
  expect(container).toBeEmptyDOMElement();
  rerender(<UntrackedSpendNote card={{}} />);
  expect(container).toBeEmptyDOMElement();
});
