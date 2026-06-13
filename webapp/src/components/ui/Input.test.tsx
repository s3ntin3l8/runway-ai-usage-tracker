import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Input, Label, HelperText } from './Input';

describe('Input', () => {
  it('renders and accepts typed text', async () => {
    render(<Input placeholder="Email" />);
    const input = screen.getByPlaceholderText('Email');
    await userEvent.type(input, 'hello');
    expect(input).toHaveValue('hello');
  });

  it('forwards the disabled prop', () => {
    render(<Input placeholder="x" disabled />);
    expect(screen.getByPlaceholderText('x')).toBeDisabled();
  });

  it('merges a custom className', () => {
    render(<Input placeholder="x" className="extra" />);
    expect(screen.getByPlaceholderText('x')).toHaveClass('extra');
  });
});

describe('Label', () => {
  it('renders its children', () => {
    render(<Label>Name</Label>);
    expect(screen.getByText('Name')).toBeInTheDocument();
  });
});

describe('HelperText', () => {
  it('renders subtle text by default', () => {
    render(<HelperText>hint</HelperText>);
    expect(screen.getByText('hint')).toHaveClass('text-fg-subtle');
  });

  it('renders critical text when error is set', () => {
    render(<HelperText error>bad</HelperText>);
    expect(screen.getByText('bad')).toHaveClass('text-critical');
  });
});
