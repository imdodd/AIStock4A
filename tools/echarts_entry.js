// AIStock4A ECharts 按需构建入口（产出 frontend/echarts.min.js）
//
// 前端实际使用：
//   API:     echarts.init / getInstanceByDom / graphic.LinearGradient
//   series:  line / bar / candlestick
//   option:  grid / tooltip / legend / dataZoom / axisPointer /
//            title / markLine / markPoint / markArea
//
// 复现构建（在仓库根目录执行，依赖装进被 gitignore 的 .tmp/）：
//   mkdir -p .tmp/echarts-build
//   npm install --prefix .tmp/echarts-build --no-audit --no-fund echarts@5.5.0 esbuild
//   cp tools/echarts_entry.js .tmp/echarts-build/entry.js
//   cd .tmp/echarts-build
//   npx esbuild entry.js --bundle --minify --format=iife --target=es2018 \
//     --legal-comments=inline \
//     --banner:js='/*! ECharts v5.5.0 | Apache-2.0 | AIStock4A 按需构建版: 仅含 line/bar/candlestick + 10 组件 + CanvasRenderer */' \
//     --outfile=../../frontend/echarts.min.js
//
// 注意：页面用 <script src> 以全局变量 echarts 引入（非 ES module），
//       因此必须 window.echarts = echarts；不要改成 ESM 输出。
//       若将来新增漏斗图/仪表盘等，在下方 use([...]) 中补对应 Chart/Component 即可。
import * as echarts from 'echarts/core';
import { LineChart, BarChart, CandlestickChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,
  MarkPointComponent,
  MarkAreaComponent,
  TitleComponent,
  AxisPointerComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart, BarChart, CandlestickChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  MarkLineComponent, MarkPointComponent, MarkAreaComponent,
  TitleComponent, AxisPointerComponent,
  CanvasRenderer,
]);

window.echarts = echarts;
