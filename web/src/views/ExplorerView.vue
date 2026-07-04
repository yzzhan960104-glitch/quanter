<script setup lang="ts">
/**
 * 因子探索沙盒视图（路由 /explorer）
 *
 * 两图：
 *   ① 多空分层累计收益（Q1-Q5 灰阶渐变 + LS 多空 Alpha 高亮粗线）
 *   ② IC 时序柱状（正红负绿）+ 20 日滚动均值折线（黄）+ IC 分布直方图（副图）
 *
 * 数据流：submitGrid → task_id → 轮询 getResult（500ms × 120 = 60s 上限）
 *        → markRaw 写 shallowRef → setOption。
 *
 * 红线：万级数据 shallowRef + markRaw（防深 reactive）；轮询定时器
 *      onBeforeUnmount 清理（防内存泄漏）。
 */
import { ref, shallowRef, onBeforeUnmount, computed, markRaw } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { submitGrid, getResult, type FactorGridSpec, type FactorGridResult } from '../api/explorer'
import { logger } from '../utils/logger'

// 固定因子下拉（与 factors 模块导出函数名对齐）
const FACTORS = [
  { label: '横截面动量', value: 'cross_sectional_momentum' },
  { label: '波动率调整动量', value: 'vol_adjusted_momentum' },
  { label: '北向资金动量', value: 'north_flow_momentum' },
  { label: '龙虎榜信号', value: 'dragon_signal' },
  { label: '横截面估值', value: 'valuation_cross_section' },
]

const form = ref({
  factor: 'cross_sectional_momentum',
  dateRange: ['2024-01-02', '2024-06-30'] as string[],
})
const loading = ref(false)
// shallowRef：海量时序不深 reactive（性能红线）
const result = shallowRef<FactorGridResult | null>(null)

let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollCount = 0
const POLL_MAX = 120
const POLL_INTERVAL = 500

function clearPoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null }
  pollCount = 0
}

onBeforeUnmount(clearPoll)   // 防内存泄漏：离开页面前必清定时器

async function pollResult(taskId: string) {
  pollCount++
  if (pollCount > POLL_MAX) {
    ElMessage.warning('因子计算超时（60s），请稍后重试或缩短区间')
    loading.value = false
    return
  }
  try {
    const p = await getResult(taskId)
    if (p.ready && p.result) {
      result.value = p.result
      loading.value = false
      if (p.result.ok) {
        ElMessage.success(`IC均值=${p.result.ic_mean.toFixed(3)} IR=${p.result.ic_ir.toFixed(2)}`)
      } else {
        ElMessage.warning(p.result.reason || '因子计算无可用数据')
      }
      return
    }
    pollTimer = setTimeout(() => pollResult(taskId), POLL_INTERVAL)
  } catch (e: any) {
    logger.error('轮询因子结果失败:', e)
    loading.value = false
    ElMessage.error('因子结果轮询失败')
  }
}

async function onSubmit() {
  clearPoll()
  loading.value = true
  result.value = null
  const spec: FactorGridSpec = {
    factor: form.value.factor,
    universe: ['dynamic_top50'],   // 固定活跃池标识，后端解析
    start: form.value.dateRange[0],
    end: form.value.dateRange[1],
  }
  try {
    const r: any = await submitGrid(spec)
    if (r.degraded && r.result) {
      // Redis 宕机降级：线程池同步执行完，结果直接返回
      result.value = r.result
      loading.value = false
    } else {
      pollResult(r.task_id)
    }
  } catch (e: any) {
    loading.value = false
    ElMessage.error('因子网格提交失败：' + (e?.message || ''))
  }
}

// ============ 图①：多空分层累计收益 ============
const lsChartOption = computed(() => {
  const r = result.value
  if (!r || !r.ok) return null
  const dates = r.dates
  // Q1 浅 → Q5 深（灰阶渐变），LS 多空 Alpha 高亮 Quant 蓝粗线
  const qColors = ['#b2b5be', '#8e939d', '#d29922', '#26a69a', '#2962ff']
  type LineSeries = {
    name: string; type: 'line'; data: number[];
    smooth: boolean; symbol: string; lineStyle: { width: number; color: string }
  }
  const series: LineSeries[] = (['Q1', 'Q2', 'Q3', 'Q4', 'Q5'] as const).map((k, i) => ({
    name: k, type: 'line', data: r.quantile_nav[k] ?? [],
    smooth: true, symbol: 'none',
    lineStyle: { width: 1.5, color: qColors[i] },
  }))
  series.push({
    name: 'Q5-Q1 Alpha', type: 'line', data: r.quantile_nav.LS ?? [],
    smooth: true, symbol: 'none', lineStyle: { width: 2.5, color: '#2962ff' },
  })
  return markRaw({
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: ['Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q5-Q1 Alpha'], top: 0 },
    grid: { left: 60, right: 30, top: 40, bottom: 50 },
    xAxis: { type: 'category', data: dates, axisLabel: { formatter: (v: string) => String(v).slice(0, 7) } },
    yAxis: { type: 'value', name: '累计净值', scale: true },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series,
  })
})

// ============ 图②：IC 时序柱+滚动均值折线 + 直方图（双 grid） ============
const icChartOption = computed(() => {
  const r = result.value
  if (!r || !r.ok) return null
  const dates = r.dates
  const ic = r.ic_series
  // 20 日滚动 IC 均值（不足 19 日窗口为 null）
  const ma: (number | null)[] = ic.map((_, i) => {
    if (i < 19) return null
    const win = ic.slice(i - 19, i + 1)
    return win.reduce((a, b) => a + b, 0) / win.length
  })
  // 直方图 x 轴：bin 索引（bin_edges 长度 = counts 长度 + 1）
  const histLabels = r.ic_hist.bin_edges.slice(0, -1).map((e, i) =>
    `${e.toFixed(2)}~${r.ic_hist.bin_edges[i + 1]?.toFixed(2) ?? ''}`,
  )
  return markRaw({
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: ['逐期IC', '20日均值'], top: 0 },
    grid: [
      { left: 60, right: 30, top: 40, height: '55%' },          // 主图：柱+线
      { left: 60, right: 30, top: '68%', height: '28%' },        // 副图：直方图
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0,
        axisLabel: { formatter: (v: string) => String(v).slice(0, 7) } },
      { type: 'category', data: histLabels, gridIndex: 1,
        axisLabel: { show: false } },
    ],
    yAxis: [
      { type: 'value', name: 'IC', gridIndex: 0 },
      { type: 'value', name: '频次', gridIndex: 1 },
    ],
    series: [
      {
        name: '逐期IC', type: 'bar', xAxisIndex: 0, yAxisIndex: 0, data: ic,
        // 正红负绿（A 股配色），visualMap 按值染色
        itemStyle: { color: (p: any) => (p.value >= 0 ? '#ef5350' : '#26a69a') },
      },
      {
        name: '20日均值', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: ma,
        smooth: true, symbol: 'none', connectNulls: true,
        lineStyle: { width: 2, color: '#d29922' },
      },
      {
        name: 'IC分布', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: r.ic_hist.counts, itemStyle: { color: '#2962ff' },
      },
    ],
  })
})
</script>

<template>
  <div class="explorer-shell">
    <!-- 顶部参数条 -->
    <div class="param-bar">
      <el-select v-model="form.factor" placeholder="因子" style="width: 200px">
        <el-option v-for="f in FACTORS" :key="f.value" :label="f.label" :value="f.value" />
      </el-select>
      <el-date-picker
        v-model="form.dateRange" type="daterange" value-format="YYYY-MM-DD"
        start-placeholder="开始" end-placeholder="结束" style="width: 280px"
      />
      <el-button type="primary" :loading="loading" @click="onSubmit">提交因子网格</el-button>
      <span v-if="result && result.ok" class="summary">
        IC均值={{ result.ic_mean.toFixed(3) }} | IR={{ result.ic_ir.toFixed(2) }}
        | t={{ result.t_stat.toFixed(2) }}{{ Math.abs(result.t_stat) > 2 ? ' (显著)' : '' }}
      </span>
    </div>

    <!-- 图① 多空分层 -->
    <section v-if="lsChartOption" class="chart-card">
      <div class="chart-title">多空分层累计收益（Q5-Q1 纯净 Alpha 高亮）</div>
      <v-chart class="chart" :option="lsChartOption" autoresize theme="terminal-dark" />
    </section>

    <!-- 图② IC 时序+分布 -->
    <section v-if="icChartOption" class="chart-card">
      <div class="chart-title">IC 时序（柱 + 20 日均值）与分布直方图</div>
      <v-chart class="chart" :option="icChartOption" autoresize theme="terminal-dark" />
    </section>

    <!-- 空态 -->
    <div v-if="!lsChartOption && !loading" class="empty">
      提交因子网格后在此显示分层收益与 IC 分析
    </div>
  </div>
</template>

<style scoped>
.explorer-shell { padding: 12px; height: 100%; overflow: auto; background: #131722; }
.param-bar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.summary { font-size: 12px; color: #26a69a; font-family: ui-monospace, Menlo, monospace; }
.chart-card {
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px;
  margin-bottom: 12px; padding: 8px;
}
.chart-title { font-size: 13px; color: #d1d4dc; margin-bottom: 6px; }
.chart { height: 380px; }
.empty { color: #6e7681; padding: 40px; text-align: center; }
</style>
