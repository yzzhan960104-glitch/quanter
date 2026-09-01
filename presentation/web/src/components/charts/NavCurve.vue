<!--
  NavCurve 收益率曲线族（可视化重构 P1 · 2026-09-01；同日需求④改收益率同图对比）。

  物理意图：回答"钱赚不赚，跑赢大盘没有"——上格双腿累计收益（较入金基数 %）与
  上证/纳指/标普三基准同图（各按自身 era 首日归一），下格主腿回撤水下图，
  dataZoom 联动。x 轴=基准文件的 A 股交易日轴（净值日 ⊆ 该轴；基准线在净值
  空缺日不画点）。数据=nav_history + benchmarks 快照（时代口径见 ops/nav_history.py）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>累计收益率 <span class="sub">（era 起 · 与基准同图）</span></span>
        <span v-if="updated" class="sub">至 {{ updated }}</span>
      </div>
    </template>
    <v-chart v-if="days.length" class="chart" :option="option" theme="terminal-light" autoresize />
    <el-empty v-else description="净值历史累积中（自 era 起每交易日一点）" :image-size="60" />
    <div v-if="preEraNote" class="pre-era">{{ preEraNote }}</div>
  </el-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { LineChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { use } from 'echarts/core'
import type { NavHistory, NavDay, Benchmarks } from '../../api/home'

use([LineChart, GridComponent, TooltipComponent, LegendComponent,
     DataZoomComponent, CanvasRenderer])

const props = defineProps<{ history: NavHistory; benchmarks?: Benchmarks | null }>()

const days = computed(() => props.history.days ?? [])
const base = computed(() => props.history.base || 200000)
const updated = computed(() => {
  const d = days.value
  return d.length ? d[d.length - 1].date : ''
})
const preEraNote = computed(() => props.history.pre_era_note || '')

/** x 轴：基准 A 股交易日轴优先（净值日并入并集，防基准文件缺当日）。 */
const axis = computed<string[]>(() => {
  const a = props.benchmarks?.axis ?? []
  const extra = days.value.map((d) => d.date).filter((d) => !a.includes(d))
  return Array.from(new Set([...a, ...extra])).sort()
})

const navByDate = computed<Map<string, NavDay>>(() =>
  new Map(days.value.map((d) => [d.date, d])))

/** 腿 → 轴对齐的累计收益 %（轴上无净值日 → null 断点）。 */
function seriesOf(leg: string): Array<number | null> {
  return axis.value.map((d) => {
    const nav = navByDate.value.get(d)?.legs?.[leg]
    return nav != null ? +((nav / base.value - 1) * 100).toFixed(3) : null
  })
}

/** 基准 → 轴对齐累计收益 %（较自身首个非空点归一）。 */
function benchSeries(idx: number): Array<number | null> {
  const s = props.benchmarks?.series?.[idx]
  if (!s || !props.benchmarks) return []
  const axisB = props.benchmarks.axis
  const first = s.points.find((p) => p != null)
  if (first == null) return []
  const byDate = new Map(axisB.map((d, i) => [d, s.points[i]]))
  return axis.value.map((d) => {
    const v = byDate.get(d)
    return v != null ? +((v / first - 1) * 100).toFixed(3) : null
  })
}

/** 主腿回撤 %（较轴内前高）。 */
function drawdownOf(leg: string): Array<number | null> {
  let peak = -Infinity
  return axis.value.map((d) => {
    const nav = navByDate.value.get(d)?.legs?.[leg]
    if (nav == null) return null
    peak = Math.max(peak, nav)
    return +((nav / peak - 1) * 100).toFixed(3)
  })
}

const option = computed(() => {
  const hasBench = !!props.benchmarks?.series?.length
  const legend = ['主腿', '实验腿', ...(hasBench
    ? (props.benchmarks!.series.map((s) => s.name)) : [])]
  // 基准弱化样式：细线+低调灰阶（腿是主角）
  const benchStyle = (color: string) => ({
    type: 'line', showSymbol: false, lineStyle: { width: 1, color, opacity: 0.85 },
    itemStyle: { color }, emphasis: { focus: 'series' }, connectNulls: true, z: 1,
  })
  const benchColors = ['#86909c', '#d29922', '#bc8cff']
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v}%` },
    legend: { data: legend, top: 0 },
    grid: [
      { left: 48, right: 16, top: 28, height: '52%' },
      { left: 48, right: 16, top: '74%', height: '16%' },
    ],
    xAxis: [
      { type: 'category', data: axis.value, boundaryGap: false },
      { type: 'category', gridIndex: 1, data: axis.value, boundaryGap: false,
        axisLabel: { show: false } },
    ],
    yAxis: [
      { type: 'value', axisLabel: { formatter: '{value}%' },
        splitLine: { lineStyle: { color: '#eef1f6' } } },
      { type: 'value', gridIndex: 1, max: 0, axisLabel: { formatter: '{value}%' },
        splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }],
    series: [
      { name: '主腿', type: 'line', data: seriesOf('main'), showSymbol: true,
        symbolSize: 5, lineStyle: { width: 2.5 }, emphasis: { focus: 'series' },
        connectNulls: true, z: 3 },
      { name: '实验腿', type: 'line', data: seriesOf('exp'), showSymbol: true,
        symbolSize: 5, lineStyle: { width: 2, type: 'dashed' },
        emphasis: { focus: 'series' }, connectNulls: true, z: 2 },
      ...((props.benchmarks?.series ?? []).map((s, i) => ({
        name: s.name, data: benchSeries(i), ...benchStyle(benchColors[i % 3]),
      }))),
      { name: '回撤(主腿)', type: 'line', xAxisIndex: 1, yAxisIndex: 1,
        data: drawdownOf('main'), areaStyle: { opacity: 0.3 }, showSymbol: false,
        lineStyle: { width: 1, color: '#26a69a' }, itemStyle: { color: '#26a69a' } },
    ],
  }
})
</script>

<style scoped>
.chart { height: 360px; width: 100%; }
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.pre-era { color: var(--el-text-color-secondary); font-size: 11px; margin-top: 6px; }
</style>
