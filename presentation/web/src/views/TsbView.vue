<!--
  TsbView 机会观察（2026-09-02 用户需求）。

  三图联动（窗口 YTD/1Y/3Y/全部 × 刻度三态）。刻度语义（2026-09-02 用户需求：
  看指标间涨跌的相对同步关系，又不让高波动指标把低波动压成平线）：
    相对归一（默认）：起点锚定——所有线从 0 出发，±100=各自窗口内最大涨/跌
      幅度（等幅不压制 + 起点统一，方向形状保真）；
    对数涨跌 / 线性涨跌：窗口锚归一的真实涨跌%（跨指标量级可比）：
    图一：紫金矿业 × 纽约金（COMEX 主力）——金股/金价相对强弱
    图二：美元指数 × 美债10Y × 纽约金——利率-美元-金三角
    图三：海南橡胶 × 沪胶主力连续——胶股/胶价相对强弱
  各序列交易日历不同（A 股/COMEX/外汇/美债），轴取并集、缺数日 null 断点
  connectNulls 平滑。数据源注记见页脚（DXY=六成分对子自算）。
-->
<template>
  <div class="tsb">
    <div class="head">
      <h2>机会观察</h2>
      <div class="ctrl">
        <el-segmented v-model="mode" :options="modeOptions" size="small" />
        <el-segmented v-model="win" :options="winOptions" size="small" />
      </div>
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
const mode = ref<'relative' | 'log' | 'linear'>('relative')
const modeOptions = [
  { label: '相对归一', value: 'relative' },
  { label: '对数涨跌', value: 'log' }, { label: '线性涨跌', value: 'linear' },
]
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

/** 序列 → 轴对齐值（三态）：relative=窗口区间位 0-100（各自 min-max 归一，
 *  等幅看形态同步）；log=净值因子（对数轴）；linear=窗口锚归一 %。 */
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
  // 窗口内因子（含锚定语义）：factor = v/anchor
  const factors = axis.map((d) => {
    const v = byDate.get(d)
    return (v != null && d >= cutoff.value) ? v / anchor : null
  })
  if (mode.value !== 'relative') {
    return factors.map((f) => f == null ? null
      : mode.value === 'log' ? +f.toFixed(4) : +((f - 1) * 100).toFixed(2))
  }
  // 相对归一（起点锚定）：所有线从 0 出发，窗口内最大涨或跌=±100——
  // 既统一起点（09-02 用户问"起点为什么不是0"），又等幅不互相压制。
  // 语义=各自最大幅度的百分比；形状/方向保真，符号不变。
  const devs = factors.filter((f): f is number => f != null)
    .map((f) => (f - 1) * 100)
  if (!devs.length) return factors.map(() => null)
  const maxAbs = Math.max(...devs.map(Math.abs))
  if (maxAbs < 1e-9) return factors.map(() => 0)
  return factors.map((f) => f == null ? null
    : +(((f - 1) * 100 / maxAbs) * 100).toFixed(1))
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

/** 轴对齐的真实涨跌 %（tooltip 用：相对模式下主值是区间位，% 是第二读数）。 */
function pctAligned(key: keyof TsbDoc['series'], axis: string[]): Array<number | null> {
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

function mkOption(keys: Array<keyof TsbDoc['series']>, axis: string[]) {
  const pcts = new Map(keys.map((k) => [k, pctAligned(k, axis)]))
  const idxOf = (d: string) => axis.indexOf(d)
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: Array<{ seriesName: string; value: number | null
       ; axisValue: string; seriesId: string }>) => {
        const rows = (params || []).map((pp) => {
          const k = keys.find((x) => NAME[x] === pp.seriesName) as keyof TsbDoc['series']
          const i = idxOf(pp.axisValue)
          const pct = pcts.get(k)?.[i]
          const main = pp.value == null ? '—'
            : mode.value === 'relative' ? `${pp.value.toFixed(0)}`
            : mode.value === 'log' ? `${((pp.value - 1) * 100).toFixed(2)}%`
            : `${pp.value.toFixed(2)}%`
          const sub = (mode.value === 'relative' && pct != null)
            ? ` <span style="color:#86909c">(${pct > 0 ? '+' : ''}${pct}%)</span>` : ''
          return `<span style="display:inline-block;width:8px;height:8px;border-radius:4px;background:${STYLE[k]?.color ?? '#999'};margin-right:6px"></span>${pp.seriesName}：${main}${sub}`
        })
        return `<b>${params?.[0]?.axisValue ?? ''}</b><br/>` + rows.join('<br/>')
      },
    },
    legend: { top: 0, data: keys.map((k) => NAME[k]) },
    grid: { left: 52, right: 16, top: 30, bottom: 28 },
    xAxis: { type: 'category', data: axis, boundaryGap: false,
             axisLine: { onZero: false } },
    yAxis: (mode.value === 'relative'
      ? { type: 'value', min: -112, max: 112,
          // ±100=各自窗口最大幅度，恰在边界会被切半——上下各留呼吸带
          //（09-02 两次用户反馈："最高最低不在图里"+"起点为什么不是0"）
          axisLabel: { formatter: (v: number) =>
            (Math.abs(v) <= 100) ? String(v) : '' },
          name: '相对幅度（起点0 · ±100=各自窗口最大涨跌）',
          nameTextStyle: { color: '#86909c' },
          splitLine: { lineStyle: { color: '#eef1f6' } } }
      : { type: mode.value === 'log' ? 'log' : 'value', logBase: 10,
          axisLabel: { formatter: (v: number) => mode.value === 'log'
            ? `${((v - 1) * 100) > 0 ? '+' : ''}${((v - 1) * 100).toFixed(0)}%`
            : `${v > 0 ? '+' : ''}${v}%` },
          splitLine: { lineStyle: { color: '#eef1f6' } } }),
    dataZoom: [{ type: 'inside' }],
    series: keys.map((k, ki) => ({
      name: NAME[k], type: 'line', data: align(k, axis),
      showSymbol: false, connectNulls: true, emphasis: { focus: 'series' },
      lineStyle: { color: STYLE[k].color, width: STYLE[k].width,
                   type: STYLE[k].dash === 'dashed' ? 'dashed' : 'solid' },
      itemStyle: { color: STYLE[k].color },
      // 首序列挂 0 基准线（相对模式下=起点/涨跌分界，灰虚线不抢戏）
      ...(ki === 0 ? { markLine: {
        silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#c0c4cc', type: 'dashed', width: 1 },
        data: mode.value === 'relative' ? [{ yAxis: 0 }] : [],
      } } : {}),
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
.ctrl { display: flex; gap: 10px; align-items: center; }
.head h2 { margin: 0; }
.chart { height: 340px; width: 100%; }
.foot { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 10px;
        color: var(--el-text-color-secondary); font-size: 11px; }
</style>
