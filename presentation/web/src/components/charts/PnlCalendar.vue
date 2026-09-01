<!--
  PnlCalendar 日收益热力日历（可视化重构 P1 · 2026-09-01）。

  物理意图：GitHub 贡献图形态的日盈亏日历——一眼看战斗节奏（红=赚/A股惯例、
  绿=亏）。日收益=当日nav/前日nav-1（首日较入金基数）；缺腿数据的日期不落格。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>日收益日历 <span class="sub">（{{ legLabel }}）</span></span>
        <el-select v-model="leg" size="small" style="width: 96px">
          <el-option label="主腿" value="main" />
          <el-option label="实验腿" value="exp" />
        </el-select>
      </div>
    </template>
    <v-chart v-if="cells.length" class="chart" :option="option" theme="terminal-light" autoresize />
    <el-empty v-else description="日收益累积中" :image-size="60" />
  </el-card>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import VChart from 'vue-echarts'
import { HeatmapChart } from 'echarts/charts'
import { CalendarComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { use } from 'echarts/core'
import type { NavHistory } from '../../api/home'

use([HeatmapChart, CalendarComponent, TooltipComponent, VisualMapComponent, CanvasRenderer])

const props = defineProps<{ history: NavHistory }>()
const leg = ref<'main' | 'exp'>('main')
const legLabel = computed(() => (leg.value === 'main' ? '主腿' : '实验腿'))

/** [(date, 日收益%)]：首日较 base，其后较前日（前日缺 → 较 base）。 */
const cells = computed<[string, number][]>(() => {
  const out: [string, number][] = []
  let prev: number | null = null
  for (const d of props.history.days ?? []) {
    const nav = d.legs?.[leg.value]
    if (nav == null) { prev = null; continue }
    const anchor = prev ?? props.history.base
    out.push([d.date, +((nav / anchor - 1) * 100).toFixed(3)])
    prev = nav
  }
  return out
})

const option = computed(() => ({
  tooltip: { formatter: (p: { data: [string, number] }) =>
    `${p.data[0]}<br/>日收益 <b>${p.data[1]}%</b>` },
  visualMap: {
    min: -3, max: 3, calculable: false, orient: 'horizontal', left: 'center', bottom: 0,
    itemHeight: 90, textStyle: { color: '#86909c' },
    // A 股惯例：红=赚 绿=亏；中性灰=停牌/缺数据邻域
    inRange: { color: ['#26a69a', '#f0f2f5', '#ef5350'] },
  },
  calendar: {
    range: cells.value.length
      ? [cells.value[0][0], cells.value[cells.value.length - 1][0]] : [],
    cellSize: ['auto', 16],
    left: 40, right: 12, top: 24,
    itemStyle: { color: '#ffffff', borderColor: '#f5f7fa', borderWidth: 2 },
    yearLabel: { show: false },
    dayLabel: { color: '#86909c', firstDay: 1 },
    monthLabel: { color: '#86909c' },
    splitLine: { lineStyle: { color: '#dcdfe6' } },
  },
  series: [{
    type: 'heatmap', coordinateSystem: 'calendar', data: cells.value,
  }],
}))
</script>

<style scoped>
.chart { height: 200px; width: 100%; }
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
</style>
