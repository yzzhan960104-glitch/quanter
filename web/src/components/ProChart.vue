<script setup lang="ts">
/**
 * 专业 K 线图：主图蜡烛（OHLCV）+ 净值叠加线（右轴）+ 副图成交量 +
 * 买卖点 markPoint（trades.direction）。主副图 dataZoom 联动。
 *
 * 数据来源：消费父组件传入的 ohlcv / navSeries / trades 三条序列，
 * 全部为只读快照，本组件不做任何回测计算，纯展示。
 *
 * 暗色主题：由 main.ts 注册的 'terminal-dark' 提供，<v-chart theme> 直接引用。
 */
import { computed, markRaw } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkPointComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { OhlcvPoint, NavPoint, TradeRecord } from '@/api/backtest'

// 按需注册 ECharts 组件：蜡烛/折线/柱状三种 series + 五个 component + Canvas 渲染器
use([
  CandlestickChart, LineChart, BarChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkPointComponent, CanvasRenderer,
])

const props = defineProps<{
  ohlcv: OhlcvPoint[]
  navSeries: NavPoint[]
  trades: TradeRecord[]
}>()

/**
 * 净值按日期建索引（Map O(1) 查找），用于把 nav 对齐到 K 线 x 轴。
 *
 * Why：K 线交易日与净值日期理论上同源（均出自后端同一时间序列），
 * 但防御性地用 Map + null 兜底——一旦某交易日缺失 nav，折线自然断开，
 * 避免错误地连线（前视偏差/插值污染）。
 */
const navByDate = computed(() => {
  const m = new Map<string, number>()
  for (const p of props.navSeries) m.set(p.date, p.nav)
  return m
})

/**
 * ECharts 完整 option。
 *
 * markRaw 隔离：option 内含数百~数千根 K 线的数值数组 + tooltip/markPoint 闭包，
 * 用 markRaw 阻止 Vue 对其做深度响应式代理（否则会递归代理整棵配置树，
 * 既浪费内存又会污染 echarts.setOption 期望的纯对象契约）。
 * 数据本体由父组件 shallowRef 持有，本组件只在切片变化时整体重算。
 */
const option = computed(() => {
  // x 轴共用日期序列（主图、副图都消费同一份 dates，保证 dataZoom 联动对齐）
  const dates = props.ohlcv.map((o) => o.date)
  // ECharts 蜡烛数据顺序严格为 [open, close, low, high]，与后端字段命名不同，注意映射
  const candles = props.ohlcv.map((o) => [o.open, o.close, o.low, o.high])
  const volumes = props.ohlcv.map((o) => o.volume)
  // 净值按 K 线日期取值，缺失日给 null（折线在该点断开，不做插值）
  const navLine = props.ohlcv.map((o) => navByDate.value.get(o.date) ?? null)

  // K 线交易日集合：用于过滤买卖点，保证 markPoint 的 coord 日期一定能被 category x 轴定位。
  // 不变量：买卖点日期须为 K 线交易日，否则 category 轴无法定位（markPoint 渲染失败/错位）。
  const datesSet = new Set(dates)
  // 买卖点 markPoint：coord=[日期, 价格]；B 绿（买入）/ S 红（卖出）
  // 只接受方向明确的 buy/sell 记录，且日期必须落在 K 线类目集内，过滤掉任何异常项
  const markPoints = props.trades
    .filter((t) => (t.direction === 'buy' || t.direction === 'sell') && datesSet.has(t.date))
    .map((t) => ({
      // coord 第一个元素为 category x 轴的类目值（日期字符串），第二为 y 值（价格）
      coord: [t.date, t.price],
      value: t.direction === 'buy' ? 'B' : 'S',
      itemStyle: { color: t.direction === 'buy' ? '#3fb950' : '#ef5350' },
      label: { color: '#fff', fontSize: 10 },
    }))

  return markRaw({
    // 关闭动画：K 线点数多，动画既卡顿又干扰对历史行情的静态研判
    animation: false,
    legend: { top: 0, data: ['K线', '净值', '成交量'] },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    // 跨主副图的十字光标联动（x 轴同步指示）
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    // 双 grid 布局：主图占上方 58%，副图（成交量）占下方 18%，留出 dataZoom 空间
    grid: [
      { left: '6%', right: '6%', top: '8%', height: '58%' },   // 主图：蜡烛 + 净值叠加
      { left: '6%', right: '6%', top: '74%', height: '18%' },  // 副图：成交量
    ],
    xAxis: [
      // 主图 x 轴：category（日期）。min/max='dataMin'/'dataMax' 让两端贴边显示完整区间
      { type: 'category', data: dates, scale: true, boundaryGap: true, axisLine: { onZero: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
      // 副图 x 轴：show:false（日期标签由主图承担），但 data 必须与主图一致以保证联动对齐
      { type: 'category', gridIndex: 1, data: dates, show: false, min: 'dataMin', max: 'dataMax' },
    ],
    yAxis: [
      { scale: true, splitArea: { show: false } },             // 价格轴（左，蜡烛）scale:true 不含零轴
      // 净值轴：显式 position:'right' 落在主图右侧，否则 ECharts 默认把多条 yAxis 堆在
      // 同一 grid 左侧，价格轴与净值轴会压叠在一起无法分辨。
      // gridIndex:0 显式绑定主图 grid（蜡烛所在），splitLine 关闭避免与价格轴网格交叉污染。
      { scale: true, position: 'right', gridIndex: 0, splitLine: { show: false } },
      { scale: true, gridIndex: 1, splitNumber: 2 },           // 成交量轴（副图 gridIndex 1）
    ],
    // dataZoom 联动：inside（鼠标滚轮/拖拽）+ slider（底部缩略条），
    // xAxisIndex:[0,1] 让主副图同步缩放——独立缩放会导致价格与量能错位
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], top: '94%', height: 16, start: 60, end: 100 },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: candles, xAxisIndex: 0, yAxisIndex: 0,
        // 买卖点挂在主图蜡烛上（coord 指向 K 线 x 轴日期 + 价格 y 值）
        markPoint: { data: markPoints, symbolSize: 28 },
      },
      {
        name: '净值', type: 'line', data: navLine, xAxisIndex: 0, yAxisIndex: 1,
        // symbol:none 隐藏数据点，仅留折线；smooth 轻微平滑视觉降噪
        smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#58a6ff' },
      },
      {
        name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 2,
        itemStyle: { color: '#30363d' },
      },
    ],
  })
})
</script>

<template>
  <v-chart class="pro-chart" :option="option" theme="terminal-dark" autoresize />
</template>

<style scoped>
/* 撑满父容器；高度由父级布局给定，确保与终端暗色面板无缝贴合 */
.pro-chart {
  width: 100%;
  height: 100%;
}
</style>
