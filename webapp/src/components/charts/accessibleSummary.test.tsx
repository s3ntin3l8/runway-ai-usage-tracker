import { render } from '@testing-library/react';
import { AccessibleSummary } from './accessibleSummary';

describe('AccessibleSummary', () => {
  it('renders a caption and a list of label/value pairs', () => {
    const { getByText } = render(
      <AccessibleSummary
        caption="Token composition"
        entries={[
          { label: 'Input', value: '1K' },
          { label: 'Output', value: '2K' },
        ]}
      />,
    );
    expect(getByText('Token composition')).toBeInTheDocument();
    expect(getByText('Input: 1K')).toBeInTheDocument();
    expect(getByText('Output: 2K')).toBeInTheDocument();
  });

  it('renders the note and the details of an entry', () => {
    const { getByText } = render(
      <AccessibleSummary
        caption="Ranking"
        entries={[
          {
            label: 'project-a',
            value: '100',
            note: 'via anthropic',
            details: [{ label: 'Input', value: '60' }],
          },
        ]}
      />,
    );
    expect(getByText('project-a: 100 (via anthropic)')).toBeInTheDocument();
    expect(getByText('Input: 60')).toBeInTheDocument();
  });

  it('renders only the caption when there is nothing to list', () => {
    const { container, queryAllByRole } = render(
      <AccessibleSummary caption="Token composition" entries={[]} />,
    );
    expect(container.textContent).toBe('Token composition');
    expect(queryAllByRole('listitem')).toHaveLength(0);
  });
});
