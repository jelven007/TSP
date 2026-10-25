// Register only the series/components used by the application. Keep all runtime
// imports here so a new chart cannot silently restore the full ECharts bundle.
import { use as registerECharts } from 'echarts/core'
import { BarChart, CandlestickChart, CustomChart, HeatmapChart, LineChart, PieChart } from 'echarts/charts'
import {
  AxisPointerComponent, DataZoomComponent, GraphicComponent, GridComponent,
  LegendComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent,
  TitleComponent, TooltipComponent, VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

registerECharts([
  BarChart, CandlestickChart, CustomChart, HeatmapChart, LineChart, PieChart,
  AxisPointerComponent, DataZoomComponent, GraphicComponent, GridComponent,
  LegendComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent,
  TitleComponent, TooltipComponent, VisualMapComponent, CanvasRenderer,
])

export { init, connect, getInstanceByDom } from 'echarts/core'
export type { EChartsType as ECharts } from 'echarts/core'
export type { EChartsOption } from 'echarts'
