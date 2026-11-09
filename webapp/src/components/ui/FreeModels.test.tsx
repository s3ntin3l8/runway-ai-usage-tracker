import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { FreeModels } from './FreeModels';

it('renders the free-usage model list', () => {
  renderWithProviders(<FreeModels models={['beam', 'gpt-oss:20b']} />);
  expect(screen.getByText('Free usage applies to: beam, gpt-oss:20b')).toBeInTheDocument();
});

it('omits the line when the field is null or empty', () => {
  const { container } = renderWithProviders(<FreeModels models={null} />);
  expect(container).toBeEmptyDOMElement();
  renderWithProviders(<FreeModels models={[]} />);
  expect(screen.queryByText(/Free usage applies to/)).not.toBeInTheDocument();
});
