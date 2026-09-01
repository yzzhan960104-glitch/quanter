<!--
  NavCurve 净值曲线族（可视化重构 P1 · 2026-09-01）。

  物理意图：回答"钱赚不赚"——上格双腿净值（较入金基数 %）双线，下格回撤水下图
  （较区间高点 %），dataZoom 联动。数据=nav_history 快照（时代口径：自两腿各
  入金 20 万起算，10 万试点时代不混画——见 ops/nav_history.py 红线注）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>净值曲线 <span class="sub">（较入金 {{ (base / 10000).toFixed(0) }} 万 · %）</span></span>
        <span v-if="updated" class="sub">至 {{ updated }}</span>
      </div>
    </template>
    <v-chart v-if="days.length" class="chart" :option="option" theme="terminal-dark" autoresize />
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
  DataZoomComponent, MarkPointComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { use } from 'echarts/core'
import type { NavHistory, NavDay } from '../../api/home'

use([LineChart, GridComponent, TooltipComponent, LegendComponent,
     DataZoomComponent, MarkPointComponent, CanvasRenderer])

const props = defineProps<{ history: NavHistory }>()

const days = computed(() => props.history.days ?? [])
const base = computed(() => props.history.base || 200000)
const updated = computed(() => {
  const d = days.value
  return d.length ? d[d.length - 1].date : ''
})
const preEraNote = computed(() => props.history.pre_era_note || '')

/** 腿 → 净值 % 序列（首日较 base，其后较前日锚定的连续净值）。 */
function seriesOf(leg: string): number[] {
  return days.value.map((d: NavDay) => {
    const nav = d.legs?.[leg]
    return nav != null ? +((nav / base.value - 1) * 100).toFixed(3) : NaN
  })
}

/** 回撤 %（较区间内前高；NaN 日透传为 null 断线）。 */
function drawdownOf(leg: string): (number | null)[] {
  let peak = -Infinity
  return days.value.map((d: NavDay) => {
    const nav = d.legs?.[leg]
    if (nav == null) return null
    peak = Math.max(peak, nav)
    return +((nav / peak - 1) * 100).toFixed(3)
  })
}

const option = computed(() => {
  const ds = days.value.map((d: NavDay) => d.date)
  const main = seriesOf('main')
  const exp = seriesOf('exp')
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v}%` },
    legend: { data: ['主腿', '实验腿'], top: 0 },
    grid: [
      { left: 48, right: 16, top: 28, height: '52%' },
      { left: 48, right: 16, top: '72%', height: '18%' },
    ],
    xAxis: [
      { type: 'category', data: ds, boundaryGap: false },
      { type: 'category', gridIndex: 1, data: ds, boundaryGap: false, axisLabel: { show: false } },
    ],
    yAxis: [
      { type: 'value', axisLabel: { formatter: '{value}%' }, splitLine: { lineStyle: { color: '#2b3139' } } },
      { type: 'value', gridIndex: 1, max: 0, axisLabel: { formatter: '{value}%' },
        splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }],
    series: [
      { name: '主腿', type: 'line', data: main, showSymbol: true, symbolSize: 5,
        lineStyle: { width: 2 }, emphasis: { focus: 'series' },
        connectNulls: true, z: 3 },
      { name: '实验腿', type: 'line', data: exp, showSymbol: true, symbolSize: 5,
        lineStyle: { width: 2, type: 'dashed' }, emphasis: { focus: 'series' },
        connectNulls: true },
      { name: '回撤(主腿)', type: 'line', xAxisIndex: 1, yAxisIndex: 1,
        data: drawdownOf('main'), areaStyle: { opacity: 0.35 }, showSymbol: false,
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
