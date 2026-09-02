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
        <div class="ctrl">
          <el-button size="small" text @click="offset--">‹</el-button>
          <span class="month">{{ monthLabel }}</span>
          <el-button size="small" text :disabled="offset >= 0" @click="offset++">›</el-button>
          <el-select v-model="leg" size="small" style="width: 96px">
          <el-option label="主腿" value="main" />
          <el-option label="实验腿" value="exp" />
        </el-select>
        </div>
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
// 月度视图（09-02 用户需求）：以最新数据所在月为基准，offset 翻月（≥0 禁前翻未来）
const offset = ref(0)
const baseMonth = computed(() => {
  const ds = props.history.days ?? []
  if (ds.length) return ds[ds.length - 1].date.slice(0, 7)
  const n = new Date()
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}`
})
const monthRange = computed(() => {
  const [y, m] = baseMonth.value.split('-').map(Number)
  const d = new Date(y, m - 1 + offset.value, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
})
const monthLabel = computed(() => {
  const [y, m] = monthRange.value.split('-')
  return `${y} 年 ${Number(m)} 月`
})

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
    range: monthRange.value,
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
.ctrl { display: flex; align-items: center; gap: 4px; }
.month { font-size: 12px; color: var(--el-text-color-regular); min-width: 76px; text-align: center; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
</style>
