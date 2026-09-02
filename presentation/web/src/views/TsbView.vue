<!--
  TsbView TSB 机会观察（2026-09-02 用户需求）。

  三图联动（共享窗口选择 YTD/1Y/3Y/全部，各序列较窗口锚归一 %）：
    图一：紫金矿业 × 纽约金（COMEX 主力）——金股/金价相对强弱
    图二：美元指数 × 美债10Y × 纽约金——利率-美元-金三角
    图三：海南橡胶 × 沪胶主力连续——胶股/胶价相对强弱
  各序列交易日历不同（A 股/COMEX/外汇/美债），轴取并集、缺数日 null 断点
  connectNulls 平滑。数据源注记见页脚（DXY=六成分对子自算）。
-->
<template>
  <div class="tsb">
    <div class="head">
      <h2>TSB 机会观察</h2>
      <el-segmented v-model="win" :options="winOptions" size="small" />
    </div>

    <el-card shadow="never">
      <template #header>紫金矿业 × 纽约金（COMEX 主力）· 累计涨跌 %</template>
      <v-chart v-if="doc" class="chart" :option="opt1" theme="terminal-light" autoresize />
      <el-skeleton v-else :rows="4" animated />
    </el-card>

    <el-card shadow="never" style="margin-top: 12px">
      <template #header>美元指数 × 美债 10Y × 纽约金 · 累计涨跌 %</template>
      <v-chart v-if="doc" class="chart" :option="opt2" theme="terminal-light" autoresize />
    </el-card>

    <el-card shadow="never" style="margin-top: 12px">
      <template #header>海南橡胶 × 沪胶主力连续（RU0）· 累计涨跌 %</template>
      <v-chart v-if="doc" class="chart" :option="opt3" theme="terminal-light" autoresize />
    </el-card>

    <div class="foot">
      <span v-for="(v, k) in doc?.meta" :key="k">{{ v }}</span>
      <span v-if="doc?.updated_at">快照 {{ doc.updated_at }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import VChart from 'vue-echarts'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent }
  from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { use } from 'echarts/core'
import { getTsb, type TsbDoc, type TsbSeries } from '../api/home'

use([LineChart, GridComponent, TooltipComponent, LegendComponent,
     DataZoomComponent, CanvasRenderer])

const doc = ref<TsbDoc | null>(null)
const win = ref<'ytd' | '1y' | '3y' | 'all'>('1y')
const winOptions = [
  { label: '年初至今', value: 'ytd' }, { label: '1年', value: '1y' },
  { label: '3年', value: '3y' }, { label: '全部', value: 'all' },
]

/** 窗口起点（ISO）。 */
const cutoff = computed<string>(() => {
  const now = new Date()
  if (win.value === 'ytd') return `${now.getFullYear()}-01-01`
  if (win.value === '1y') return new Date(now.getFullYear() - 1, now.getMonth(), now.getDate()).toISOString().slice(0, 10)
  if (win.value === '3y') return new Date(now.getFullYear() - 3, now.getMonth(), now.getDate()).toISOString().slice(0, 10)
  return '0000-01-01'
})

/** 序列 → 轴对齐归一 %：锚=≤窗口首日的最近一根（窗口前的最后值，保证窗口
 *  首日的涨跌从真实前收算起）；窗口内缺数日 null（connectNulls 平滑跨节假日）。 */
function align(key: keyof TsbDoc['series'], axis: string[]):
    Array<number | null> {
  const s: TsbSeries | undefined = doc.value?.series?.[key]
  if (!s?.dates?.length) return []
  const byDate = new Map(s.dates.map((d, i) => [d, s.points[i]]))
  let anchor: number | null = null
  for (let i = 0; i < s.dates.length; i++) {
    if (s.dates[i] <= cutoff.value) anchor = s.points[i]
    else break
  }
  if (anchor == null) anchor = s.points[0]
  return axis.map((d) => {
    const v = byDate.get(d)
    return (v != null && d >= cutoff.value)
      ? +((v / anchor - 1) * 100).toFixed(2) : null
  })
}

/** 并集轴（窗口内）。 */
function unionAxis(keys: Array<keyof TsbDoc['series']>): string[] {
  const all = new Set<string>()
  for (const k of keys) {
    for (const d of doc.value?.series?.[k]?.dates ?? []) {
      if (d >= cutoff.value) all.add(d)
    }
  }
  return Array.from(all).sort()
}

const STYLE: Record<string, { color: string; width: number; dash?: 'solid' | 'dashed' }> = {
  zijin: { color: '#2962ff', width: 2.5 },
  gold: { color: '#f0b90b', width: 2 },
  dxy: { color: '#86909c', width: 2 },
  us10y: { color: '#ef5350', width: 2, dash: 'dashed' },
  rubber: { color: '#2962ff', width: 2.5 },
  rufu: { color: '#26a69a', width: 2 },
}
const NAME: Record<string, string> = {
  zijin: '紫金矿业', gold: '纽约金', dxy: '美元指数', us10y: '美债10Y',
  rubber: '海南橡胶', rufu: '沪胶主力',
}

function mkOption(keys: Array<keyof TsbDoc['series']>, axis: string[]) {
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v}%` },
    legend: { top: 0, data: keys.map((k) => NAME[k]) },
    grid: { left: 52, right: 16, top: 30, bottom: 28 },
    xAxis: { type: 'category', data: axis, boundaryGap: false },
    yAxis: { type: 'value', axisLabel: { formatter: '{value}%' },
             splitLine: { lineStyle: { color: '#eef1f6' } } },
    dataZoom: [{ type: 'inside' }],
    series: keys.map((k) => ({
      name: NAME[k], type: 'line', data: align(k, axis),
      showSymbol: false, connectNulls: true, emphasis: { focus: 'series' },
      lineStyle: { color: STYLE[k].color, width: STYLE[k].width,
                   type: STYLE[k].dash === 'dashed' ? 'dashed' : 'solid' },
      itemStyle: { color: STYLE[k].color },
    })),
  }
}

const axis1 = computed(() => (doc.value ? unionAxis(['zijin', 'gold']) : []))
const axis2 = computed(() => (doc.value ? unionAxis(['dxy', 'us10y', 'gold']) : []))
const axis3 = computed(() => (doc.value ? unionAxis(['rubber', 'rufu']) : []))
const opt1 = computed(() => mkOption(['zijin', 'gold'], axis1.value))
const opt2 = computed(() => mkOption(['dxy', 'us10y', 'gold'], axis2.value))
const opt3 = computed(() => mkOption(['rubber', 'rufu'], axis3.value))

onMounted(async () => {
  doc.value = await getTsb().catch(() => null)
})
</script>

<style scoped>
.tsb { padding: var(--qt-space-3, 12px); max-width: 1200px; margin: 0 auto; }
.head { display: flex; align-items: center; justify-content: space-between;
        margin-bottom: 12px; }
.head h2 { margin: 0; }
.chart { height: 340px; width: 100%; }
.foot { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 10px;
        color: var(--el-text-color-secondary); font-size: 11px; }
</style>
