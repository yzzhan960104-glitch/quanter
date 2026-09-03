<!--
  AllocationCard 资产构成与行业分布（全读可视化 P5.2 · 2026-09-03）。

  物理意图：钱放在哪——左格行业环形（双腿持仓按市值聚合，industry 列由快照
  管道从 stock_basic.parquet 富化进 gm_positions_*）；右格现金/市值堆叠面积
  （nav_history days[].assets，双腿合计，cash+市值=nav 逐日闭合）。缺行业映射
  的持仓归「未分类」；assets 缺段的日子断点不画（不猜数据）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>资产构成与行业分布 <span class="sub">（双腿合计）</span></span>
        <span v-if="assetTotal" class="sub">合计 {{ wan(assetTotal) }} 万</span>
      </div>
    </template>
    <div class="two-col">
      <div class="col">
        <v-chart v-if="industries.length" class="chart" :option="pieOption"
                 theme="terminal-light" autoresize />
        <el-empty v-else description="空仓（无行业分布）" :image-size="50" />
        <div class="col-note">持仓市值 {{ wan(posTotal) }} 万 · {{ industries.length }} 个行业</div>
      </div>
      <div class="col">
        <v-chart v-if="assetDays.length" class="chart" :option="areaOption"
                 theme="terminal-light" autoresize />
        <el-empty v-else description="资产构成历史累积中" :image-size="50" />
        <div class="col-note">现金+市值 = nav 逐日闭合 · 纯滚轮=翻页 / Ctrl+滚轮=缩放</div>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import VChart from 'vue-echarts'
import { PieChart, LineChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { use } from 'echarts/core'
import { getNavHistory, type NavHistory, type NavDay } from '../../api/home'
import { getPositions, getLegs, type GmPositionRow } from '../../api/gm'

use([PieChart, LineChart, GridComponent, TooltipComponent, LegendComponent,
     DataZoomComponent, CanvasRenderer])

const history = ref<NavHistory>({ base: 200000, era_start: '', days: [] })
const positions = ref<GmPositionRow[]>([])

const wan = (v: number) => (v / 10000).toFixed(1)

/** 行业聚合：双腿持仓 market_value 聚合；缺市值行退 vwap×volume=**成本口径
 *  近似（code-review F3：fpnl≠0 时与真实市值有偏，防御分支——现快照行行有
 *  market_value 不触发）。 */
const industries = computed<Array<{ name: string; value: number }>>(() => {
  const agg = new Map<string, number>()
  for (const r of positions.value) {
    const mv = Number(r.market_value ?? Number(r.vwap ?? 0) * Number(r.volume ?? 0))
    if (!Number.isFinite(mv) || mv <= 0) continue
    const ind = String(r.industry || '未分类')
    agg.set(ind, (agg.get(ind) ?? 0) + mv)
  }
  return Array.from(agg, ([name, value]) => ({ name, value: +value.toFixed(0) }))
    .sort((a, b) => b.value - a.value)
})

const posTotal = computed(() => industries.value.reduce((s, x) => s + x.value, 0))

/** 资产构成轴：assets 全段存在的日子（双腿齐）才入轴（闭合可验）。 */
const assetDays = computed<NavDay[]>(() =>
  history.value.days.filter((d) => {
    const a = d.assets ?? {}
    return ['main', 'exp'].every((leg) => a[leg]?.available != null
      && a[leg]?.market_value != null)
  }))

const cashSeries = computed(() => assetDays.value.map((d) =>
  +((d.assets!.main.available! + d.assets!.exp.available!) / 10000).toFixed(2)))
const mvSeries = computed(() => assetDays.value.map((d) =>
  +((d.assets!.main.market_value! + d.assets!.exp.market_value!) / 10000).toFixed(2)))
const assetTotal = computed(() => {
  const last = assetDays.value[assetDays.value.length - 1]
  return last ? last.assets!.main.available! + last.assets!.exp.available!
    + last.assets!.main.market_value! + last.assets!.exp.market_value! : 0
})

const pieOption = computed(() => ({
  tooltip: {
    trigger: 'item',
    valueFormatter: (v: number) => `${(v / 10000).toFixed(2)} 万`,
  },
  legend: { orient: 'vertical', right: 0, top: 'middle', type: 'scroll',
            icon: 'circle', itemWidth: 8, itemHeight: 8 },
  series: [{
    type: 'pie', radius: ['42%', '68%'], center: ['38%', '50%'],
    avoidLabelOverlap: true, padAngle: 1,
    itemStyle: { borderRadius: 3, borderWidth: 1, borderColor: '#fff' },
    label: { show: false },
    emphasis: { label: { show: true, formatter: '{b}\n{d}%' } },
    data: industries.value,
  }],
}))

const areaOption = computed(() => ({
  tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v} 万` },
  legend: { top: 0, icon: 'circle', itemWidth: 8, itemHeight: 8 },
  grid: { left: 44, right: 12, top: 26, bottom: 22 },
  xAxis: { type: 'category', boundaryGap: false,
           data: assetDays.value.map((d) => d.date) },
  yAxis: { type: 'value', axisLabel: { formatter: '{value} 万' },
           splitLine: { lineStyle: { color: '#eef1f6' } } },
  dataZoom: [{ type: 'inside', zoomOnMouseWheel: 'ctrl', moveOnMouseWheel: false }],
  series: [
    { name: '现金', type: 'line', stack: 'total', areaStyle: { opacity: 0.35 },
      showSymbol: assetDays.value.length <= 14, symbolSize: 4,
      lineStyle: { width: 1.5 }, emphasis: { focus: 'series' },
      itemStyle: { color: '#2962ff' }, data: cashSeries.value },
    { name: '持仓市值', type: 'line', stack: 'total', areaStyle: { opacity: 0.4 },
      showSymbol: assetDays.value.length <= 14, symbolSize: 4,
      lineStyle: { width: 1.5 }, emphasis: { focus: 'series' },
      itemStyle: { color: '#f0b90b' }, data: mvSeries.value },
  ],
}))

onMounted(async () => {
  try {
    history.value = await getNavHistory()
  } catch { /* 快照缺失：两图各自空态 */ }
  try {
    const legs = await getLegs()
    const all = await Promise.all(legs.map((l) => getPositions(l.key).catch(() => [])))
    positions.value = all.flat()
  } catch {
    positions.value = []
  }
})
</script>

<style scoped>
.chart { height: 260px; width: 100%; }
.two-col { display: flex; gap: 16px; flex-wrap: wrap; }
.col { flex: 1; min-width: 320px; }
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.col-note { color: var(--el-text-color-secondary); font-size: 11px;
            margin-top: 4px; text-align: center; }
</style>
