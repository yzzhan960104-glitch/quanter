<script setup lang="ts">
/**
 * 专业回测主图（三大支柱重构）：
 *   左 Y 轴 log 净值（策略 + 沪深300 基准）+ 右 Y 轴 inverse 回撤红填充 +
 *   买卖点 scatter 叠加（按数据量三档自适应 symbolSize/label，万级开 progressive）。
 *
 * 去K线决策：原 candlestick 主图信息密度过高，与净值/回撤主图冲突。新版本聚焦
 * "系统何时崩溃"（回撤水下憋气红填充）+ "买卖点细节"（scatter tooltip 含手续费/原因）。
 *
 * 数据：消费父组件 ohlcv（接口兼容保留，去K线后未使用）/ navSeries / trades /
 * benchmarkSeries，全部只读快照，纯展示。暗色主题：main.ts 注册的 'terminal-dark'。
 *
 * 性能红线：option 经 markRaw 隔离，阻止 Vue 深度代理整棵配置树（万级时序）。
 */
import { computed, markRaw } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { OhlcvPoint, NavPoint, TradeRecord, BenchmarkPoint } from '@/api/backtest'

// 按需注册：折线（净值/基准/回撤）+ 散点（买卖点）+ 四个 component + Canvas 渲染器
use([
  LineChart, ScatterChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, CanvasRenderer,
])

const props = defineProps<{
  ohlcv: OhlcvPoint[]                  // 接口兼容保留（去K线后未使用）
  navSeries: NavPoint[]
  trades: TradeRecord[]
  benchmarkSeries?: BenchmarkPoint[]   // 沪深300 ETF 归一化净值（可空）
}>()

/** 净值按日期索引（O(1) 查找），用于把 scatter 点对齐到当日净值 y 值。 */
const navByDate = computed(() => {
  const m = new Map<string, number>()
  for (const p of props.navSeries) m.set(p.date, p.nav)
  return m
})

/**
 * 买卖点 scatter 数据：叠在左轴净值线上。
 * Why y 取当日 nav 而非 trade.price：净值与回撤共享左 log 轴，scatter 也挂同一轴，
 * 用 nav 让买卖点视觉上贴着净值折线，便于看"何时建仓/平仓 vs 净值位置"。
 * 缺 nav 当日跳过该点（避免错位）。
 */
const tradePoints = computed(() => {
  const navMap = navByDate.value
  return props.trades
    .map((t) => {
      const y = navMap.get(t.date)
      if (y === undefined) return null
      return {
        value: [t.date, y],
        itemStyle: { color: t.direction === 'buy' ? '#ef5350' : '#26a69a' },  // 买红卖绿（A 股配色）
        // tooltip payload：方向/数量/价格/手续费/原因
        _dir: t.direction, _shares: t.shares, _price: t.price, _cost: t.cost,
        _reason: t.reason,
      }
    })
    .filter(Boolean) as any[]
})

/**
 * scatter 三档自适应（防御堆叠）：
 *   ≤50  点：symbolSize=10，显示 direction label
 *   ≤500 点：symbolSize=6，隐 label
 *   >500  点：symbolSize=3，隐 label，开 progressive=400 大数据渐进渲染
 */
const scatterStyle = computed(() => {
  const n = tradePoints.value.length
  if (n <= 50) return { symbolSize: 10, showLabel: true }
  if (n <= 500) return { symbolSize: 6, showLabel: false }
  return { symbolSize: 3, showLabel: false }
})

/**
 * ECharts 完整 option：双 Y 轴（左 log 净值 + 右 inverse 回撤红填充）+ scatter 买卖点。
 * markRaw：option 内含万级时序数组 + tooltip 闭包，阻止 Vue 深度代理（性能红线）。
 */
const option = computed(() => {
  const dates = props.navSeries.map((p) => p.date)
  const navVals = props.navSeries.map((p) => p.nav)

  // 基准按策略 dates 对齐（后端已 reindex+ffill，前端兜底再对齐一次防漏）
  const benchMap = new Map<string, number>()
  for (const p of props.benchmarkSeries ?? []) benchMap.set(p.date, p.nav)
  const hasBench = (props.benchmarkSeries?.length ?? 0) > 0
  const benchVals = dates.map((d) => benchMap.get(d) ?? null)

  // 回撤：累计净值派生（与 NavChart 同算法），百分比负值；inverse 轴下"水下"红填充
  const ddVals: number[] = []
  let running = 1.0
  let peak = 1.0
  for (const p of props.navSeries) {
    const r = p.return
    running = running * (1 + (isFinite(r) ? r : 0))
    if (running > peak) peak = running
    ddVals.push(peak > 0 ? ((running - peak) / peak) * 100 : 0)
  }

  const ss = scatterStyle.value
  const tp = tradePoints.value

  return markRaw({
    // 关闭动画：万级点动画既卡顿又干扰对历史净值的静态研判
    animation: false,
    legend: {
      top: 0,
      data: ['策略净值', ...(hasBench ? ['基准(沪深300)'] : []), '回撤', '买卖点'],
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (params: any[]) => {
        let html = `<b>${params[0]?.axisValue}</b><br/>`
        for (const p of params) {
          if (p.seriesName === '回撤') {
            html += `${p.marker} 回撤: ${Number(p.value).toFixed(2)}%<br/>`
          } else if (p.seriesName === '买卖点') {
            const d = p.data
            // 散点 tooltip：方向/数量@价格 + 手续费 + 风控原因（如有）
            html += `${p.marker} ${d._dir}: ${d._shares}@${Number(d._price).toFixed(2)}`
              + ` 手续费=${Number(d._cost).toFixed(2)}${d._reason ? ' (' + d._reason + ')' : ''}<br/>`
          } else if (p.value !== null && p.value !== undefined) {
            html += `${p.marker} ${p.seriesName}: ${Number(p.value).toFixed(3)}<br/>`
          }
        }
        return html
      },
    },
    grid: { left: 70, right: 70, top: 40, bottom: 60 },
    xAxis: {
      type: 'category', data: dates, boundaryGap: true,
      // 分钟级 datetime 兼容：截取前 10 字符（YYYY-MM-DD），日级原样
      axisLabel: { formatter: (v: string) => String(v).slice(0, 10) },
    },
    yAxis: [
      {
        // 左轴 log：策略净值与基准净值同币种/同起点归一化，log 轴下物理可比
        type: 'log', name: '净值(log)', position: 'left',
        axisLabel: { formatter: (v: number) => v.toFixed(2) },
      },
      {
        // 右轴 inverse：回撤百分比负值，inverse 后"水下"区域朝下，红填充直观展现痛苦期
        type: 'value', name: '回撤%', position: 'right', inverse: true,
        axisLabel: { formatter: (v: number) => `${v.toFixed(1)}%` },
      },
    ],
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', start: 0, end: 100, height: 20 },
    ],
    series: [
      {
        name: '策略净值', type: 'line', yAxisIndex: 0, data: navVals,
        smooth: true, symbol: 'none', connectNulls: true,
        lineStyle: { width: 2, color: '#2962ff' },
      },
      ...(hasBench ? [{
        name: '基准(沪深300)', type: 'line' as const, yAxisIndex: 0, data: benchVals,
        smooth: true, symbol: 'none', connectNulls: true,
        lineStyle: { width: 1.5, color: '#787b86', type: 'dashed' as const },
      }] : []),
      {
        name: '回撤', type: 'line', yAxisIndex: 1, data: ddVals,
        smooth: true, symbol: 'none',
        lineStyle: { width: 1.5, color: '#ef5350' },
        // 水下憋气：半透明红填充回撤曲线下方，直观展现"痛苦期"
        areaStyle: { color: 'rgba(239, 83, 80, 0.18)' },
      },
      {
        name: '买卖点', type: 'scatter', yAxisIndex: 0, data: tp,
        symbolSize: ss.symbolSize,
        // 万级数据开渐进渲染，避免首屏卡顿
        ...(tp.length > 500 ? { progressive: 400 } : {}),
        label: ss.showLabel
          ? { show: true, formatter: (p: any) => p.data._dir, color: '#fff', fontSize: 9 }
          : { show: false },
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
