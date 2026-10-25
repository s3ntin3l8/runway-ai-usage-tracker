// The ECharts aria component is verified, but deliberately NOT used.
//
// Verified export name in the installed echarts@6: `AriaComponent`, exported
// from 'echarts/components'. It is unsuitable here: ECharts writes the
// generated description onto `zr.dom`, which IS the container element this
// wrapper hands to echarts.init(), so it would overwrite the aria-label React
// manages — and it can only emit one flat, truncated string, not a semantic
// list of the data points.
//
// This test pins the export name so an echarts major upgrade that renames it
// is caught here rather than as a silent behaviour change.
import { AriaComponent } from 'echarts/components';

describe('echarts/components', () => {
  it('exports the aria component as AriaComponent', () => {
    expect(AriaComponent).toBeDefined();
  });
});
