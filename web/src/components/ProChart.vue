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
  MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { OhlcvPoint, NavPoint, TradeRecord } from '@/api/backtest'

// 按需注册 ECharts 组件：蜡烛/折线/柱状三种 series + 六个 component（含 MarkLine）+ Canvas 渲染器
// MarkLine（Task 18）：用于画止损/止盈/移动止损触发价的水平参考线
use([
  CandlestickChart, LineChart, BarChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkPointComponent, MarkLineComponent, CanvasRenderer,
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
 * 检测当前 ohlcv 是否为分钟级（Task 18）。
 *
 * 判定依据：取首个 date 字符串，分钟级形如 "2024-01-02 09:31:00"（含空格分隔的
 * 时分秒，或长度 >10 超出 "YYYY-MM-DD"）；日级形如 "2024-01-02"（长度恰好 10，无空格）。
 *
 * Why 只看首点而非全量扫描：ohlcv 由后端单一频率源产出，全序列同形——首点足以
 * 定性，全量扫描数千~数万根纯属浪费。后端若混频（异常）属数据契约破坏，前端
 * 不背锅，category 轴仍能容错渲染。
 */
const isMinute = computed(() => {
  const first = props.ohlcv[0]?.date ?? ''
  // 长度 >10 或含空格 → 视为分钟级 datetime（"YYYY-MM-DD HH:MM" / "YYYY-MM-DD HH:MM:SS"）
  return first.length > 10 || first.includes(' ')
})

/**
 * 风控触发点提炼（Task 18：止损/止盈/移动止损标注）。
 *
 * 数据来源契约：后端 backtest/engine.py 的 _close() 在风控平仓时，会把
 * direction 记为 "sell"（正常卖出语义），但在 trades[-1].reason 回填
 * "触及止损"/"触及止盈"/"移动止损" 等中文原因。所以「风控触发点」=
 * trades 中 direction==="sell" 且 reason 非空且含上述关键字的记录。
 *
 * Why 不用 direction==="failed"：failed 是涨跌停/资金不足导致的「未成交」
 * （见 _record_failed_trade），与止损止盈「已成交的被动平仓」语义完全不同；
 * brief 的 "direction=failed 的 reason" 描述与后端实际不符，以后端实现为准。
 *
 * 同一价位可能被多根 K 线触发（如多次触及止损），全部保留以反映真实触发次数；
 * 价格水平线（markLine）按 reason 分组去重——同 reason 的多条记录画一条线
 * 会叠成无法辨识的色块，这里改为每组 reason 仅取代表价位画一条水平参考线。
 */
const riskTriggers = computed(() => {
  // 1) 触发点明细（用于 markPoint 标注）
  const points = props.trades
    .filter(
      (t) =>
        t.direction === 'sell' &&
        typeof t.reason === 'string' &&
        t.reason.length > 0 &&
        (t.reason.includes('止损') || t.reason.includes('止盈'))
    )
    .map((t) => ({
      date: t.date,
      price: t.price,
      reason: t.reason as string,
    }))

  // 2) 水平参考线分组（按 reason 去重，每组取首个触发价作为代表线）
  //    Why 取首个而非均值：止损/止盈线是策略预设的固定价位（entry*(1-sl_pct) 等），
  //    多次触发的价位理论上应一致；若因加仓导致 entry 变化出现不同价位，取首个
  //    最贴近「首次触发」的语义，且避免均值产生不存在的「幽灵线」。
  const lineSeen = new Set<string>()   // reason 去重集合
  const lines: Array<{ reason: string; price: number }> = []
  for (const p of points) {
    if (!lineSeen.has(p.reason)) {
      lineSeen.add(p.reason)
      lines.push({ reason: p.reason, price: p.price })
    }
  }
  return { points, lines }
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
  // x 轴共用日期/时间序列（主图、副图都消费同一份，保证 dataZoom 联动对齐）
  // 分钟级时该序列为 "YYYY-MM-DD HH:MM" 字符串，category 轴对其与日级字符串同样容错
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
  // 注意：buy/sell 都会被纳入（含带 reason 的风控卖出），但风控卖出会在 riskMarkPoints
  // 里单独再标一个 SL/TP 点——两个标注共存（S 标常规卖出位、SL/TP 标风控语义），不冲突
  const markPoints = props.trades
    .filter((t) => (t.direction === 'buy' || t.direction === 'sell') && datesSet.has(t.date))
    .map((t) => ({
      // coord 第一个元素为 category x 轴的类目值（日期/时间字符串），第二为 y 值（价格）
      coord: [t.date, t.price],
      value: t.direction === 'buy' ? 'B' : 'S',
      itemStyle: { color: t.direction === 'buy' ? '#3fb950' : '#ef5350' },
      label: { color: '#fff', fontSize: 10 },
      // 买卖点尺寸（pin 默认形状，28 足够容纳单字母 B/S）
      symbolSize: 28,
    }))

  // ============ 风控触发标注（Task 18：止损/止盈水平线 + 触发点） ============
  const { points: riskPoints, lines: riskLines } = riskTriggers.value

  // markLine 数据：每条水平参考线挂在主图蜡烛 series 上，type:'max'/'min' 无法表达
  // 「指定价位的水平线」，这里用 yAxis + lineStyle 配合。ECharts markLine 的
  // yAxis 类型可在指定 y 值画一条贯穿全图的水平线，是止损止盈参考线的标准画法。
  // 颜色语义：止损=红（亏损出场）/ 止盈=绿（盈利出场），与终端日志级别配色一致。
  const markLineData = riskLines.map((l) => ({
    yAxis: l.price,
    name: l.reason,
    label: {
      formatter: l.reason,     // 线端显示原因（"触及止损"/"触及止盈"）
      position: 'end',         // 标签贴在右端，避免与 K 线主体重叠
      color: l.reason.includes('止盈') ? '#3fb950' : '#f85149',
      fontSize: 10,
    },
    lineStyle: {
      type: 'dashed',          // 虚线区分参考线与数据线
      color: l.reason.includes('止盈') ? '#3fb950' : '#f85149',
      width: 1,
    },
  }))

  // 风控触发点 markPoint：在触发根 K 线上标「SL」/「TP」/「TS」缩写
  // Why 用缩写而非中文：markPoint symbolSize 有限，中文会溢出；缩写 + tooltip
  // 悬浮显示完整 reason 是 ECharts 业界惯例（TradingView 同款）。
  const riskMarkPoints = riskPoints
    .filter((p) => datesSet.has(p.date))   // 日期须落在类目轴内，否则 coord 定位失败
    .map((p) => ({
      coord: [p.date, p.price],
      // 缩写：止损=SL(Stop Loss) / 止盈=TP(Take Profit) / 移动止损=TS(Trailing Stop)
      value: p.reason.includes('止盈') ? 'TP' : p.reason.includes('移动') ? 'TS' : 'SL',
      itemStyle: {
        color: p.reason.includes('止盈') ? '#3fb950' : '#f85149',
      },
      label: { color: '#fff', fontSize: 9 },
      // 风控点用更小尺寸（20），与买卖点（28）区分，避免遮挡
      symbolSize: 20,
    }))

  // x 轴 axisLabel：分钟级数据点密集（数千~数万根），需旋转 + interval 自适应避免重叠；
  // 日级数据点稀疏，保持水平显示更易读。
  // axisLabel.formatter：分钟级仅显示 HH:MM（日期由 dataZoom slider 承担），
  // 日级显示原样日期。Why 不用 ECharts time 轴：category 轴对字符串容错更强，
  // 不依赖 Date 解析（避免时区/格式解析失败导致轴塌陷），零回归风险。
  const minute = isMinute.value
  const xAxisLabel = minute
    ? {
        // 旋转 30° 防重叠；interval:'auto' 让 ECharts 自动稀疏化标签
        rotate: 30,
        interval: 'auto' as const,
        // 输入为完整 datetime 字符串，取空格后的 HH:MM 部分（"2024-01-02 09:31" → "09:31"）
        formatter: (val: string) => {
          const parts = String(val).split(' ')
          return parts.length > 1 ? parts[1].slice(0, 5) : val
        },
      }
    : { rotate: 0, interval: 'auto' as const }

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
      // 主图 x 轴：category（日级日期 or 分钟级 datetime 字符串）。
      // min/max='dataMin'/'dataMax' 让两端贴边显示完整区间
      {
        type: 'category', data: dates, scale: true, boundaryGap: true,
        axisLine: { onZero: false }, splitLine: { show: false },
        min: 'dataMin', max: 'dataMax',
        axisLabel: xAxisLabel,
      },
      // 副图 x 轴：show:false（标签由主图承担），但 data 必须与主图一致以保证联动对齐
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
    // 分钟级默认窗口收窄到末尾 30%（分钟级点数远多于日级，30% 仍含足够细节且首屏不卡）
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: minute ? 70 : 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], top: '94%', height: 16, start: minute ? 70 : 60, end: 100 },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: candles, xAxisIndex: 0, yAxisIndex: 0,
        // 买卖点 + 风控触发点都挂在主图蜡烛上（coord 指向 K 线 x 轴 + 价格 y 值）
        // 每个数据项内联 symbolSize（买卖点 28 / 风控点 20），避免用函数回调引入类型与兼容隐患
        markPoint: {
          data: [...markPoints, ...riskMarkPoints],
        },
        // 止损/止盈/移动止损水平参考线（Task 18）：贯穿主图的虚线，便于目测触发阈值
        markLine: {
          symbol: 'none',            // 参考线两端不画箭头（默认会有，影响视觉）
          silent: true,              // 不响应鼠标（参考线非交互元素，避免干扰 tooltip）
          data: markLineData,
        },
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
