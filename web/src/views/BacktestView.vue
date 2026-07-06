<script setup lang="ts">
/**
 * 深度归因回测视图（层级四·路由 /backtest）
 *
 * 三段编排：
 *   ① 参数条：数据集（来自 /data/datasets）+ 策略（来自 /strategies）+ 标的 + 区间 + 资金。
 *      数据集/策略下拉全部从 API 反射（反硬编码红线）；标的为用户输入（非写死 options）。
 *   ② 净值图：净值折线（log）+ 买卖点 scatter（A 股红买绿卖）。
 *   ③ 归因面板（AttributionPanel）：交易列表悬浮归因 + 最赚单笔/最大回撤切片复盘。
 *
 * 数据流：runSingleBacktest → SingleBacktestResponse → 图表 + AttributionPanel。
 */
import { ref, computed, onMounted, markRaw } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, ScatterChart } from 'echarts/charts'
import { TooltipComponent, GridComponent, LegendComponent, DataZoomComponent } from 'echarts/components'
import { runSingleBacktest, type SingleBacktestResponse, type TradeRecord } from '@/api/backtest'
import { getStrategies, type StrategyTopology } from '@/api/strategy'
import { getDatasets, type DatasetAsset } from '@/api/data'
import AttributionPanel from '@/components/AttributionPanel.vue'
import { logger } from '@/utils/logger'

use([CanvasRenderer, LineChart, ScatterChart, TooltipComponent, GridComponent, LegendComponent, DataZoomComponent])

const strategies = ref<StrategyTopology[]>([])
const datasets = ref<DatasetAsset[]>([])
const loading = ref(false)
const result = ref<SingleBacktestResponse | null>(null)

// 参数表单（数据集/策略从 API 反射；标的为用户输入）
const form = ref({
  dataset: 'daily',
  strategy: '',
  symbol: '600000.SH',
  dateRange: ['2024-01-02', '2024-06-30'] as [string, string],
  capital: 1_000_000,
  freq: '1d' as '1d' | '5m' | '1m',
})

/** 数据集 → 默认 freq 映射（选数据集时联动 freq；纯前端便捷，非业务约束） */
const DATASET_FREQ: Record<string, '1d' | '1m'> = { daily: '1d', daily_active: '1d', minute: '1m', crypto: '1m' }
function onDatasetChange(key: string) {
  const f = DATASET_FREQ[key]
  if (f) form.value.freq = f
}

onMounted(async () => {
  try {
    const [ss, ds] = await Promise.all([getStrategies(), getDatasets()])
    strategies.value = ss
    datasets.value = ds
    if (ss.length) form.value.strategy = ss[0].name
  } catch (e: any) {
    logger.error('策略/数据集列表拉取失败:', e)
  }
})

async function onRun() {
  if (!form.value.strategy) { ElMessage.warning('请先选择策略'); return }
  if (!form.value.symbol.trim()) { ElMessage.warning('请输入标的代码'); return }
  loading.value = true
  result.value = null
  try {
    result.value = await runSingleBacktest({
      symbol: form.value.symbol.trim(),
      start_date: form.value.dateRange[0],
      end_date: form.value.dateRange[1],
      initial_capital: form.value.capital,
      signal_freq: form.value.freq,
      strategy_name: form.value.strategy,
      strategy_params: {},
      freq: form.value.freq,
    })
    const m = result.value.metrics
    ElMessage.success(`回测完成：年化 ${(m.annual_return * 100).toFixed(1)}% / 回撤 ${(m.max_drawdown * 100).toFixed(1)}%`)
  } catch (e: any) {
    logger.error('回测失败:', e)
  } finally {
    loading.value = false
  }
}

/** 净值图：净值折线 + 买卖 scatter（按日期对齐到 nav） */
const navOption = computed(() => {
  const r = result.value
  if (!r) return null
  const dates = r.nav_series.map(p => p.date)
  const nav = r.nav_series.map(p => p.nav)
  // 买卖点 scatter（A 股红买绿卖）
  const buys = r.trades.filter((t: TradeRecord) => t.direction === 'buy').map(t => [t.date, t.price])
  const sells = r.trades.filter((t: TradeRecord) => t.direction === 'sell').map(t => [t.date, t.price])
  return markRaw({
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: ['净值', '买入', '卖出'], top: 0 },
    grid: { left: 60, right: 30, top: 36, bottom: 50 },
    xAxis: { type: 'category', data: dates, axisLabel: { formatter: (v: string) => v.slice(0, 7) } },
    yAxis: [
      { type: 'log', name: '净值', scale: true, splitLine: { lineStyle: { color: '#2b3139' } } },
      { type: 'value', name: '价格', scale: true, splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series: [
      { name: '净值', type: 'line', data: nav, smooth: true, symbol: 'none', lineStyle: { width: 2, color: '#2962ff' } },
      { name: '买入', type: 'scatter', yAxisIndex: 1, data: buys, symbolSize: 8, itemStyle: { color: '#ef5350' } },
      { name: '卖出', type: 'scatter', yAxisIndex: 1, data: sells, symbolSize: 8, itemStyle: { color: '#26a69a' } },
    ],
  })
})

/** 回撤图：inverse 红色填充 */
const ddOption = computed(() => {
  const r = result.value
  if (!r) return null
  const dates = r.drawdown_series.map(p => p.date)
  const dd = r.drawdown_series.map(p => (p.drawdown * 100))
  return markRaw({
    animation: false,
    tooltip: { trigger: 'axis', formatter: (p: any) => `${p[0].axisValue}<br/>回撤 ${p[0].data.toFixed(2)}%` },
    grid: { left: 60, right: 30, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: dates, axisLabel: { formatter: (v: string) => v.slice(0, 7) } },
    yAxis: { type: 'value', name: '回撤%', inverse: true, max: 0 },
    series: [{
      type: 'line', data: dd, symbol: 'none', smooth: true,
      areaStyle: { color: 'rgba(239, 83, 80, 0.25)' },
      lineStyle: { width: 1.5, color: '#ef5350' },
    }],
  })
})

/** 指标摘要条 */
const metricsLine = computed(() => {
  const r = result.value
  if (!r) return null
  const m = r.metrics
  return [
    `年化 ${(m.annual_return * 100).toFixed(1)}%`,
    `波动 ${(m.annual_volatility * 100).toFixed(1)}%`,
    `回撤 ${(m.max_drawdown * 100).toFixed(1)}%`,
    `Sharpe ${m.sharpe_ratio.toFixed(2)}`,
    `交易 ${m.n_trades} 笔`,
  ].join('  |  ')
})
</script>

<template>
  <div class="bt-view">
    <!-- 参数条：数据集 + 策略（API 反射）+ 标的 + 区间 + 资金 -->
    <div class="param-bar">
      <el-select v-model="form.dataset" placeholder="数据集" style="width: 150px" @change="onDatasetChange">
        <el-option v-for="d in datasets" :key="d.key" :label="d.name" :value="d.key" />
      </el-select>
      <el-select v-model="form.strategy" placeholder="策略" style="width: 180px">
        <el-option v-for="s in strategies" :key="s.name" :label="s.label" :value="s.name" />
      </el-select>
      <el-input v-model="form.symbol" placeholder="标的代码（如 600000.SH）" style="width: 180px" />
      <el-date-picker
        v-model="form.dateRange" type="daterange" value-format="YYYY-MM-DD"
        start-placeholder="开始" end-placeholder="结束" style="width: 260px"
      />
      <el-input-number v-model="form.capital" :min="10000" :step="100000" :controls="false" style="width: 130px" />
      <el-select v-model="form.freq" style="width: 80px">
        <el-option label="日级" value="1d" />
        <el-option label="5分" value="5m" />
        <el-option label="1分" value="1m" />
      </el-select>
      <el-button type="primary" :loading="loading" @click="onRun">运行回测</el-button>
    </div>

    <div v-if="metricsLine" class="metrics-strip">{{ metricsLine }}</div>

    <!-- 净值图 + 回撤图 -->
    <template v-if="result">
      <section class="chart-card">
        <div class="chart-title">净值曲线 + 买卖点（红买 / 绿卖）</div>
        <v-chart v-if="navOption" class="chart" :option="navOption" autoresize theme="terminal-dark" />
      </section>
      <section class="chart-card">
        <div class="chart-title">回撤时序</div>
        <v-chart v-if="ddOption" class="chart small" :option="ddOption" autoresize theme="terminal-dark" />
      </section>

      <!-- 归因面板 -->
      <AttributionPanel :trades="result.trades" :drawdown="result.drawdown_series" />
    </template>

    <div v-else-if="!loading" class="empty">配置参数后点击「运行回测」，结果含归因面板与切片复盘</div>
  </div>
</template>

<style scoped>
.bt-view { flex: 1; overflow: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.param-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.metrics-strip {
  font-size: 12px; color: #26a69a; font-family: ui-monospace, Menlo, monospace;
  background: #1e222d; border: 1px solid #2b3139; border-radius: 4px; padding: 6px 10px;
}
.chart-card { background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px; }
.chart-title { font-size: 12px; color: #d1d4dc; margin-bottom: 4px; }
.chart { height: 340px; }
.chart.small { height: 180px; }
.empty { color: #787b86; padding: 48px; text-align: center; font-size: 12px; }
</style>
