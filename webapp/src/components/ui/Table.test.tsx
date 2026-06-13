import { render, screen } from '@testing-library/react';
import { Table, THead, TBody, TR, TH, TD } from './Table';

describe('Table', () => {
  it('renders a full table composition', () => {
    render(
      <Table>
        <THead>
          <TR>
            <TH>Model</TH>
            <TH>Tokens</TH>
          </TR>
        </THead>
        <TBody>
          <TR>
            <TD>Opus</TD>
            <TD>1.2M</TD>
          </TR>
        </TBody>
      </Table>,
    );
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Model' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'Opus' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '1.2M' })).toBeInTheDocument();
  });

  it('renders the correct table element tags', () => {
    const { container } = render(
      <Table className="t">
        <THead className="h">
          <TR className="r">
            <TH className="th">H</TH>
          </TR>
        </THead>
        <TBody className="b">
          <TR>
            <TD className="td">D</TD>
          </TR>
        </TBody>
      </Table>,
    );
    expect(container.querySelector('table.t')).toBeInTheDocument();
    expect(container.querySelector('thead.h')).toBeInTheDocument();
    expect(container.querySelector('tbody.b')).toBeInTheDocument();
    expect(container.querySelector('tr.r')).toBeInTheDocument();
    expect(container.querySelector('th.th')).toBeInTheDocument();
    expect(container.querySelector('td.td')).toBeInTheDocument();
  });
});
