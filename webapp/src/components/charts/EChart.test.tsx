import { render } from '@testing-library/react';

const init = vi.fn();
const setOption = vi.fn();
const resize = vi.fn();
const dispose = vi.fn();

// echarts/core touches canvas (absent in jsdom); mock the whole tree-shaken bundle.
vi.mock('echarts/core', () => ({
  init: (...args: unknown[]) => {
    init(...args);
    return { setOption, resize, dispose, on: vi.fn() };
  },
  use: vi.fn(),
}));
vi.mock('echarts/charts', () => ({
  BarChart: {},
  CustomChart: {},
  HeatmapChart: {},
  LineChart: {},
  PieChart: {},
  ScatterChart: {},
}));
vi.mock('echarts/components', () => ({
  DataZoomComponent: {},
  GridComponent: {},
  LegendComponent: {},
  MarkAreaComponent: {},
  MarkLineComponent: {},
  TooltipComponent: {},
  VisualMapComponent: {},
}));
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }));

import { EChart } from './EChart';

describe('EChart', () => {
  beforeEach(() => {
    init.mockClear();
    setOption.mockClear();
    resize.mockClear();
    dispose.mockClear();
  });

  it('renders a container div and merges the className', () => {
    const { container } = render(<EChart option={{}} className="h-40" />);
    const div = container.firstChild as HTMLElement;
    expect(div.tagName).toBe('DIV');
    expect(div).toHaveClass('h-40');
    expect(div).toHaveClass('w-full');
  });

  it('initializes the chart and pushes the option', () => {
    const option = { series: [{ type: 'pie', data: [{ name: 'a', value: 1 }] }] };
    render(<EChart option={option} />);
    expect(init).toHaveBeenCalledOnce();
    expect(setOption).toHaveBeenCalledWith(option, { notMerge: true });
  });

  it('passes the chart instance to onReady', () => {
    const onReady = vi.fn();
    render(<EChart option={{}} onReady={onReady} />);
    expect(onReady).toHaveBeenCalledOnce();
    expect(onReady.mock.calls[0][0]).toMatchObject({ setOption });
  });

  it('disposes the chart on unmount', () => {
    const { unmount } = render(<EChart option={{}} />);
    unmount();
    expect(dispose).toHaveBeenCalledOnce();
  });

  it('honors notMerge=false', () => {
    render(<EChart option={{ a: 1 }} notMerge={false} />);
    expect(setOption).toHaveBeenLastCalledWith({ a: 1 }, { notMerge: false });
  });
});
