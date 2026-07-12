<script setup lang="ts">
/**
 * 蔡森形态学审核大屏（Phase 3 · Task 7）
 *
 * 三栏布局（复用 --qt-* token + Element Plus，与 LiveCockpitView 同构）：
 *   ① 左栏：候选计划列表（ElTable，按 rr_ratio 降序，徽章 pattern_type/status）
 *   ② 右栏：lightweight-charts K 线图（candles + markers 形态点 + priceLines 止损止盈/颈线）
 *   ③ 底部：参数表单 + scan/approve/reject/activate 按钮 + 回放 tab
 *
 * 顶部操作动线（蔡森流水线审核 SOP）：
 *   scan 触发筛选 → 刷新候选列表 → 选中 → 看 K 线 → approve/reject/微调 → activate
 *
 * 红线（CLAUDE.md 量化风控·边界审查）：
 *   - lightweight-charts 实例 onBeforeUnmount 必销毁（防 canvas 内存泄漏）；
 *   - 切换选中计划时先 remove 旧 priceLines 再画新（防残留虚线堆叠）；
 *   - markers 经 createSeriesMarkers（v5 推荐入口，setMarkers v6 将移除）；
 *   - 状态完全跟随后端返回值，前端不本地推断（杜绝"虚假繁荣"）；
 *   - activate 前 el-popconfirm 二次确认（ARMED 后挂单待执行，不可撤销）。
 *
 * lightweight-charts v5 API 适配（已验证 v5.2.0 typings）：
 *   - chart.addSeries(CandlestickSeries, options)（非 v4 的 addCandlestickSeries）
 *   - createSeriesMarkers(series, markers)（非 v4 的 series.setMarkers）
 *   - series.createPriceLine(options)（v4/v5 通用）
 */
import { ref, shallowRef, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createChart, CandlestickSeries, createSeriesMarkers,
  type IChartApi, type ISeriesApi, type IPriceLine, type Time,
  type SeriesMarker, type CreatePriceLineOptions,
  type ISeriesMarkersPluginApi,
  LineStyle,                                   // v5 价位线样式枚举（Solid/Dotted/Dashed/...）
  type LineWidth,                              // v5 价位线宽度字面量联合类型（1|2|3|4）
} from 'lightweight-charts'
// ECharts（年化收益曲线，vue-echarts 按需注册）
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, TitleComponent } from 'echarts/components'
import VChart from 'vue-echarts'
use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, TitleComponent])
import {
  scan, listPlans, getChart, reviewPlan, activatePlan, runReplay, getConfigSchema,
  type CandidatePlan, type ChartData, type ReplayReport, type ScanRequestBody,
  type EquityPoint, type Trade,
} from '../api/caisen'
import { logger } from '../utils/logger'

/**
 * 图表配色防御性回退常量。
 *
 * 物理意图：initChart 取 --qt-* token 计算值作为 lightweight-charts 配色（与 terminal.css
 * 极夜黑对齐）。当 getComputedStyle 因样式未加载 / token 误删返回空串时，回落到这套
 * 与极夜黑主题视觉一致的 hex 兜底，避免图表黑底白字漂移。仅为防御性回退，正常链路走 token。
 */
const FALLBACK_COLORS = {
  bg: '#1e222d',                               // 卡片底色（--qt-bg-card 回退）
  text: '#d1d4dc',                             // 主文本（--qt-text-primary 回退）
  grid: '#2b3139',                             // 网格 / 边框（--qt-border 回退）
  up: '#ef5350',                               // 涨色（A 股红，--qt-up 回退）
  down: '#26a69a',                             // 跌色（A 股绿，--qt-down 回退）
} as const

// ============ 状态 ============
const plans = shallowRef<CandidatePlan[]>([])
const selectedPlan = shallowRef<CandidatePlan | null>(null)
const chartData = shallowRef<ChartData | null>(null)
const loadingPlans = ref(false)
const scanning = ref(false)
const loadingChart = ref(false)
const reviewing = ref(false)
const activating = ref(false)
const replaying = ref(false)
const replayReport = shallowRef<ReplayReport | null>(null)
const activeTab = ref<'review' | 'replay'>('review')
// 回测跑通批次：策略参数 schema（反射渲染参数表单 + 规则清单）+ cfg_override（用户调参，随 replay 提交）
const configSchema = shallowRef<Record<string, any>>({})
const cfgOverride = ref<Record<string, any>>({})

// 扫描参数表单（简化版 ScanRequest：date 默认今日，universe 默认宽基，cfg_override 留高级输入）
const today = new Date().toISOString().slice(0, 10)
// universe 在表单层用 string（逗号分隔）承载，提交时 split 转数组——
// 镜像 replayForm.universe 的模式（el-input v-model 绑数组会导致输入/显示异常，破坏 scan 入口）。
const scanForm = ref({
  date: today,
  universe: '510300.SH,510050.SH,510500.SH,159915.SZ',
  cfg_override: {} as Record<string, unknown>,
})

// 回放参数表单
const replayForm = ref({
  start: today,
  end: today,
  universe: '' as string,                    // 空字符串 = 全市场（后端 null）
})

// 审核 edits 微调表单（仅 approve 时提交，绑定选中计划字段）
const editForm = ref({
  stop_loss: 0,
  take_profit: 0,
  take_profit_2x: 0,
})

// ============ 候选列表排序：按 rr_ratio 降序（高风险优先审核） ============
const sortedPlans = computed(() =>
  [...plans.value].sort((a, b) => b.rr_ratio - a.rr_ratio)
)

// ============ lightweight-charts 实例（shallowRef 避免深层响应式包装） ============
const chartContainer = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let markersApi: ISeriesMarkersPluginApi<Time> | null = null
let activePriceLines: IPriceLine[] = []      // 当前已添加的价位线，切换计划时逐个 remove

// ============ 图表初始化 ============
/**
 * 挂载 lightweight-charts 实例：createChart(container) → addSeries(Candlestick)。
 *
 * Why 暗色主题对齐 terminal.css 极夜黑：chart 配置的 background/text/grid 直接取
 * --qt-* token 计算值（getComputedStyle），避免裸 hex 漂移。
 */
function initChart() {
  if (!chartContainer.value || chart) return
  const cs = getComputedStyle(document.documentElement)
  // 取 --qt-* token 计算值；token 丢失（空串）时回落 FALLBACK_COLORS 防御性 hex
  const bg = cs.getPropertyValue('--qt-bg-card').trim() || FALLBACK_COLORS.bg
  const text = cs.getPropertyValue('--qt-text-primary').trim() || FALLBACK_COLORS.text
  const grid = cs.getPropertyValue('--qt-border').trim() || FALLBACK_COLORS.grid
  const up = cs.getPropertyValue('--qt-up').trim() || FALLBACK_COLORS.up
  const down = cs.getPropertyValue('--qt-down').trim() || FALLBACK_COLORS.down

  chart = createChart(chartContainer.value, {
    autoSize: true,
    layout: {
      background: { color: bg },
      textColor: text,
      fontFamily: cs.getPropertyValue('--qt-font-sans').trim() || undefined,
    },
    grid: {
      vertLines: { color: grid },
      horzLines: { color: grid },
    },
    rightPriceScale: { borderColor: grid },
    timeScale: { borderColor: grid, timeVisible: false },
    crosshair: { mode: 0 },                  // Normal 模式（十字线随鼠标）
  })
  candleSeries = chart.addSeries(CandlestickSeries, {
    upColor: up,
    downColor: down,
    borderUpColor: up,
    borderDownColor: down,
    wickUpColor: up,
    wickDownColor: down,
  })
}

/** 销毁 lightweight-charts 实例（防 canvas 内存泄漏） */
function destroyChart() {
  if (chart) {
    chart.remove()
    chart = null
    candleSeries = null
    markersApi = null
    activePriceLines = []
  }
}

/**
 * 渲染图表：消费 ChartData 契约（candles/markers/priceLines）。
 *
 * 红线：
 *   1. 切换计划前清空旧 priceLines（防残留虚线堆叠）；
 *   2. candles 为空（data_lake 降级）时跳过 setData，仅画 priceLines；
 *   3. markers 用 createSeriesMarkers（v5 入口），v4 的 setMarkers v6 将移除。
 */
function renderChart(data: ChartData) {
  if (!chart || !candleSeries) return
  // 清旧 priceLines（每次切换计划重新画）
  activePriceLines.forEach((pl) => candleSeries!.removePriceLine(pl))
  activePriceLines = []

  // K 线（time 需转为 lightweight-charts 接受的 Time 类型）
  if (data.candles.length > 0) {
    candleSeries.setData(
      data.candles.map((c) => ({ time: c.time as Time, open: c.open, high: c.high, low: c.low, close: c.close }))
    )
    chart.timeScale().fitContent()
  } else {
    // 无 K 线数据（price_lake 未接）：清空 + 提示
    candleSeries.setData([])
  }

  // 标记（形态点：W 底四点 / 突破 / 回踩 / 止损触发）
  const markers: SeriesMarker<Time>[] = data.markers.map((m) => ({
    time: m.time as Time,
    position: m.position,
    color: m.color,
    shape: m.shape,
    text: m.text,
  }))
  // createSeriesMarkers 首次调用创建 plugin，后续用 .setMarkers() 更新（v5 推荐模式）
  if (markersApi) {
    markersApi.setMarkers(markers)
  } else {
    markersApi = createSeriesMarkers(candleSeries, markers)
  }

  // 价位线（止损/止盈/颈线/突破价/满足点水平虚线）
  // PriceLine.lineWidth/lineStyle 后端产出 number，需映射为 lightweight-charts v5 的
  // 枚举/字面量类型（LineWidth=1|2|3|4 / LineStyle 枚举）；用枚举常量显式转换，杜绝 as any。
  data.priceLines.forEach((pl) => {
    const opts: CreatePriceLineOptions = {
      price: pl.price,
      color: pl.color,
      lineWidth: (pl.lineWidth ?? 1) as LineWidth,
      lineStyle: (pl.lineStyle ?? LineStyle.Dashed) as LineStyle,
      axisLabelVisible: pl.axisLabelVisible ?? true,
      title: pl.title ?? '',
    }
    activePriceLines.push(candleSeries!.createPriceLine(opts))
  })
}

// ============ 选中计划联动：加载 chart + 同步 editForm ============
watch(selectedPlan, async (plan) => {
  if (!plan) {
    chartData.value = null
    return
  }
  // 同步 edits 表单为当前计划字段（审核微调起点）
  editForm.value = {
    stop_loss: plan.stop_loss,
    take_profit: plan.take_profit,
    take_profit_2x: plan.take_profit_2x,
  }
  // 拉图表数据
  loadingChart.value = true
  try {
    const data = await getChart(plan.plan_id)
    chartData.value = data
    await nextTick()                          // 确保 DOM 已渲染
    renderChart(data)
  } catch (e: any) {
    logger.error('加载图表失败:', e)
    ElMessage.error('加载图表失败：' + (e?.message || ''))
  } finally {
    loadingChart.value = false
  }
})

function onSelectPlan(row: CandidatePlan) {
  selectedPlan.value = row
}

// ============ 操作：扫描 / 审核 / 激活 / 回放 ============
async function onScan() {
  scanning.value = true
  try {
    // 表单层 universe 为逗号分隔字符串，提交时拆分为标的数组传后端（与后端 ScanRequest.universe: string[] 对齐）
    const payload: ScanRequestBody = {
      date: scanForm.value.date,
      universe: scanForm.value.universe
        .split(/[\s,，]+/)
        .map((s) => s.trim())
        .filter(Boolean),
      cfg_override: scanForm.value.cfg_override,
    }
    const result = await scan(payload)
    ElMessage.success(`扫描完成，命中 ${result.length} 个候选计划`)
    await refreshPlans()
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || ''
    ElMessage.error('扫描失败：' + detail)
  } finally {
    scanning.value = false
  }
}

async function refreshPlans() {
  loadingPlans.value = true
  try {
    plans.value = await listPlans()
  } catch (e: any) {
    logger.error('加载候选列表失败:', e)
  } finally {
    loadingPlans.value = false
  }
}

async function onReview(action: 'approve' | 'reject') {
  if (!selectedPlan.value) return
  reviewing.value = true
  try {
    const edits = action === 'approve'
      ? {
          stop_loss: editForm.value.stop_loss,
          take_profit: editForm.value.take_profit,
          take_profit_2x: editForm.value.take_profit_2x,
        }
      : {}
    const updated = await reviewPlan(selectedPlan.value.plan_id, { action, edits })
    ElMessage.success(action === 'approve' ? '已通过审核（APPROVED）' : '已驳回（REJECTED）')
    // 局部更新选中计划 + 列表（无需全量刷新）
    selectedPlan.value = updated
    plans.value = plans.value.map((p) => (p.plan_id === updated.plan_id ? updated : p))
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || ''
    ElMessage.error('审核失败：' + detail)
  } finally {
    reviewing.value = false
  }
}

async function onActivate() {
  if (!selectedPlan.value) return
  activating.value = true
  try {
    const updated = await activatePlan(selectedPlan.value.plan_id)
    ElMessage.success('已激活（ARMED）挂单待执行')
    selectedPlan.value = updated
    plans.value = plans.value.map((p) => (p.plan_id === updated.plan_id ? updated : p))
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || ''
    ElMessage.error('激活失败：' + detail)
  } finally {
    activating.value = false
  }
}

async function onReplay() {
  replaying.value = true
  try {
    // cfg_override：只提交用户实际改过的参数（非空），空值用 StrategyConfig 默认
    const overrides: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(cfgOverride.value)) {
      if (v !== '' && v !== null && v !== undefined) overrides[k] = v
    }
    const body = {
      start: replayForm.value.start,
      end: replayForm.value.end,
      universe: replayForm.value.universe.trim()
        ? replayForm.value.universe.split(/[\s,，]+/).filter(Boolean)
        : null,
      cfg_override: overrides,
    }
    replayReport.value = await runReplay(body)
    ElMessage.success(`回放完成，命中 ${replayReport.value.n_hits} 笔`)
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || ''
    ElMessage.error('回放失败：' + detail)
  } finally {
    replaying.value = false
  }
}

// ============ 生命周期 ============
onMounted(async () => {
  initChart()
  await refreshPlans()
  // 拉策略参数 schema（反射渲染参数表单 + 规则清单，#2/#4 同源）
  try {
    configSchema.value = await getConfigSchema()
  } catch (e: any) {
    logger.error('加载策略参数 schema 失败:', e)
  }
})

onBeforeUnmount(() => {
  destroyChart()
})

// ============ 辅助：徽章配色（按 pattern_type / status） ============
/** 形态类型徽章：W 底蓝、头肩顶橙、其他灰 */
function patternTagType(p: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (p === 'w_bottom') return 'success'
  if (p === 'head_shoulder') return 'warning'
  if (p === 'triangle_bottom') return 'danger'
  return 'info'
}

/** 形态类型中文名 */
function patternLabel(p: string): string {
  if (p === 'w_bottom') return 'W 底'
  if (p === 'head_shoulder') return '头肩底'
  if (p === 'triangle_bottom') return '收敛三角'
  return p
}

/** 状态徽章配色（状态机当前态） */
function statusTagType(s: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  switch (s) {
    case 'PENDING_APPROVAL': return 'warning'
    case 'APPROVED': return 'success'
    case 'ARMED': return 'danger'            // ARMED 高危（挂单待执行）用红
    case 'FILLED': return 'success'
    case 'CLOSED': return 'info'
    case 'REJECTED': return 'info'
    default: return ''
  }
}

/** 选中计划是否可激活（仅 APPROVED 态可推进到 ARMED） */
const canActivate = computed(() => selectedPlan.value?.status === 'APPROVED')
/** 选中计划是否可审核（仅 PENDING_APPROVAL 态可 approve/reject） */
const canReview = computed(() => selectedPlan.value?.status === 'PENDING_APPROVAL')

// 回放月度收益排序（用于柱状展示，按月份时间序）
const sortedMonthlyReturns = computed(() => {
  if (!replayReport.value) return []
  return Object.entries(replayReport.value.monthly_returns)
    .map(([month, rr]) => ({ month, rr }))
    .sort((a, b) => a.month.localeCompare(b.month))
})

// 形态分布（回放报告里的形态命中数）
const patternDistEntries = computed(() => {
  if (!replayReport.value) return []
  return Object.entries(replayReport.value.pattern_dist)
})

// 策略参数表单：反射 configSchema.properties → [{name, type, description, default, min, max}]
// 物理意图：前端动态渲染参数输入（#4 参数可调），description 同时是规则说明（#2 规则列举）。
const schemaParams = computed(() => {
  const props = (configSchema.value?.properties || {}) as Record<string, any>
  return Object.entries(props).map(([name, spec]) => ({
    name,
    type: spec.type as string,
    description: (spec.description || spec.title || '') as string,
    default: spec.default,
    minimum: spec.minimum,
    maximum: spec.maximum,
  }))
})

// 年化收益曲线 ECharts option（equity_curve → LineSeries；标题标年化 CAGR）
const equityChartOption = computed(() => {
  const curve = replayReport.value?.equity_curve || []
  const ann = ((replayReport.value?.annualized_return || 0) * 100).toFixed(2)
  return {
    title: { text: `资金曲线（年化 ${ann}%）`, left: 'center', textStyle: { color: '#d1d4dc', fontSize: 13 } },
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: curve.map((p: EquityPoint) => (p.date || '').slice(0, 10)), axisLabel: { color: '#888' } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#888', formatter: (v: number) => v.toFixed(2) } },
    series: [{
      name: 'equity', type: 'line', data: curve.map((p: EquityPoint) => p.equity),
      smooth: true, lineStyle: { color: '#0066cc', width: 2 }, areaStyle: { color: 'rgba(0,102,204,0.15)' },
    }],
  }
})
</script>

<template>
  <div class="caisen-shell">
    <!-- 顶部标题条 -->
    <div class="top-bar">
      <span class="title">蔡森形态学 · T 日候选审核</span>
      <span class="subtitle">scan → 选中 → 看图 → approve → activate</span>
      <el-button
        size="small" type="primary" :loading="scanning"
        @click="onScan"
      >触发扫描</el-button>
      <el-button
        size="small" plain :loading="loadingPlans"
        @click="refreshPlans"
      >刷新列表</el-button>
    </div>

    <!-- 主体：左列表 + 右图表 -->
    <div class="main-area">
      <!-- 左栏：候选计划列表 -->
      <section class="plans-card">
        <div class="chart-title">候选计划（按盈亏比降序，{{ plans.length }} 个）</div>
        <el-table
          :data="sortedPlans" size="small" empty-text="暂无候选（点击「触发扫描」生成）"
          highlight-current-row
          @current-change="onSelectPlan"
          max-height="100%"
        >
          <el-table-column label="标的" prop="symbol" width="110" />
          <el-table-column label="形态" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="patternTagType(row.pattern_type)">
                {{ patternLabel(row.pattern_type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="盈亏比" width="75">
            <template #default="{ row }">
              <span class="rr-value">{{ row.rr_ratio.toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="突破价" width="75">
            <template #default="{ row }">{{ row.breakout_price.toFixed(2) }}</template>
          </el-table-column>
          <el-table-column label="止损" width="75">
            <template #default="{ row }">
              <span class="loss-text">{{ row.stop_loss.toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTagType(row.status)">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="成立日" min-width="95">
            <template #default="{ row }">
              <span class="mono">{{ row.formed_at.slice(0, 10) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <!-- 右栏：lightweight-charts K 线图 -->
      <section class="chart-card">
        <div class="chart-title">
          <span v-if="selectedPlan">
            {{ selectedPlan.symbol }} · {{ patternLabel(selectedPlan.pattern_type) }}
            · 盈亏比 {{ selectedPlan.rr_ratio.toFixed(2) }}
          </span>
          <span v-else class="hint">从左侧选择候选计划查看 K 线</span>
          <span v-if="chartData && chartData.candles.length === 0" class="warn-text">
            （price_data 未装配，仅显示价位线）
          </span>
        </div>
        <div ref="chartContainer" class="chart-container" v-loading="loadingChart"></div>

        <!-- 选中计划的关键参数面板 -->
        <div v-if="selectedPlan" class="plan-detail">
          <div class="detail-row">
            <span class="dk">突破价</span><span class="dv">{{ selectedPlan.breakout_price.toFixed(3) }}</span>
            <span class="dk">颈线</span><span class="dv">{{ selectedPlan.neckline_price.toFixed(3) }}</span>
            <span class="dk">谷底</span><span class="dv">{{ selectedPlan.bottom_price.toFixed(3) }}</span>
            <span class="dk">回踩区间</span>
            <span class="dv">{{ selectedPlan.entry_lower.toFixed(3) }} ~ {{ selectedPlan.entry_upper.toFixed(3) }}</span>
          </div>
          <div class="detail-row">
            <span class="dk">止盈·一</span><span class="dv up">{{ selectedPlan.take_profit.toFixed(3) }}</span>
            <span class="dk">止盈·二</span><span class="dv up">{{ selectedPlan.take_profit_2x.toFixed(3) }}</span>
            <span class="dk">止损</span><span class="dv down">{{ selectedPlan.stop_loss.toFixed(3) }}</span>
            <span class="dk">分配股数</span><span class="dv">{{ selectedPlan.shares }}</span>
            <span class="dk">有效至</span><span class="dv mono">{{ selectedPlan.valid_until.slice(0, 10) }}</span>
          </div>
        </div>
      </section>
    </div>

    <!-- 底部：审核/回放双 tab -->
    <section class="bottom-card">
      <el-tabs v-model="activeTab" class="bottom-tabs">
        <!-- Tab 1：参数表单 + 审核/激活 -->
        <el-tab-pane label="扫描参数 & 审核" name="review">
          <div class="bottom-grid">
            <!-- 扫描参数 -->
            <div class="form-block">
              <div class="block-title">扫描参数（ScanRequest）</div>
              <el-form size="small" label-width="70px">
                <el-form-item label="扫描日">
                  <el-date-picker
                    v-model="scanForm.date" type="date" value-format="YYYY-MM-DD"
                    style="width: 160px"
                  />
                </el-form-item>
                <el-form-item label="标的池">
                  <el-input
                    v-model="scanForm.universe"
                    placeholder="逗号分隔，如 510300.SH,510050.SH"
                    style="width: 320px"
                  />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" :loading="scanning" @click="onScan">触发扫描</el-button>
                </el-form-item>
              </el-form>
            </div>

            <!-- 审核操作 -->
            <div class="form-block" v-if="selectedPlan">
              <div class="block-title">
                审核操作（{{ selectedPlan.symbol }} · 当前态 {{ selectedPlan.status }}）
              </div>
              <!-- edits 微调（仅 PENDING_APPROVAL 态可调） -->
              <el-form size="small" label-width="80px" :disabled="!canReview">
                <el-form-item label="止损">
                  <el-input-number
                    v-model="editForm.stop_loss" :min="0" :precision="3" :step="0.1" style="width: 140px"
                  />
                </el-form-item>
                <el-form-item label="止盈·一">
                  <el-input-number
                    v-model="editForm.take_profit" :min="0" :precision="3" :step="0.1" style="width: 140px"
                  />
                </el-form-item>
                <el-form-item label="止盈·二">
                  <el-input-number
                    v-model="editForm.take_profit_2x" :min="0" :precision="3" :step="0.1" style="width: 140px"
                  />
                </el-form-item>
                <el-form-item>
                  <el-button
                    type="success" :loading="reviewing" :disabled="!canReview"
                    @click="onReview('approve')"
                  >通过（APPROVED）</el-button>
                  <el-button
                    type="danger" plain :loading="reviewing" :disabled="!canReview"
                    @click="onReview('reject')"
                  >驳回（REJECTED）</el-button>
                  <el-popconfirm
                    title="确认激活？计划将进入 ARMED 态，挂单待执行（不可撤销）。"
                    confirm-button-text="激活" cancel-button-text="取消"
                    @confirm="onActivate"
                  >
                    <template #reference>
                      <el-button
                        type="danger" :loading="activating" :disabled="!canActivate"
                      >激活挂单（ARMED）</el-button>
                    </template>
                  </el-popconfirm>
                </el-form-item>
              </el-form>
              <span v-if="!canReview" class="hint">（仅 PENDING_APPROVAL 态可审核）</span>
              <span v-else-if="!canActivate" class="hint">（通过审核后可激活）</span>
            </div>
            <div class="form-block empty-block" v-else>
              <span class="hint">从左侧选择候选计划进行审核</span>
            </div>
          </div>
        </el-tab-pane>

        <!-- Tab 2：回放 -->
        <el-tab-pane label="历史回放" name="replay">
          <div class="replay-area">
            <div class="form-block">
              <div class="block-title">回放参数（ReplayRequest）</div>
              <el-form size="small" label-width="60px" inline>
                <el-form-item label="起始">
                  <el-date-picker
                    v-model="replayForm.start" type="date" value-format="YYYY-MM-DD"
                    style="width: 150px"
                  />
                </el-form-item>
                <el-form-item label="结束">
                  <el-date-picker
                    v-model="replayForm.end" type="date" value-format="YYYY-MM-DD"
                    style="width: 150px"
                  />
                </el-form-item>
                <el-form-item label="标的池">
                  <el-input
                    v-model="replayForm.universe"
                    placeholder="留空=全市场（慢）；建议填 30-100 只标的快回放"
                    style="width: 320px"
                  />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" :loading="replaying" @click="onReplay">运行回放</el-button>
                </el-form-item>
              </el-form>
            </div>

            <!-- 策略参数表单（反射 schema 动态渲染，#2 规则列举 + #4 参数可调 同源）-->
            <div class="form-block">
              <el-collapse>
                <el-collapse-item name="cfg">
                  <template #title>
                    <span class="block-title">策略参数（展开调参 · {{ schemaParams.length }} 个 · 默认零命中请放宽）</span>
                  </template>
                  <div class="cfg-grid">
                    <div v-for="p in schemaParams" :key="p.name" class="cfg-item">
                      <span class="cfg-label" :title="p.description">{{ p.name }}</span>
                      <el-switch
                        v-if="p.type === 'boolean'" v-model="cfgOverride[p.name]" size="small"
                      />
                      <el-input-number
                        v-else v-model="cfgOverride[p.name]" size="small"
                        :min="p.minimum" :max="p.maximum"
                        :step="p.type === 'integer' ? 1 : 0.1"
                        :precision="p.type === 'integer' ? 0 : 3"
                        style="width: 130px"
                      />
                    </div>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </div>

            <!-- 回放结果 -->
            <div v-if="replayReport" class="replay-report">
              <div class="report-grid">
                <div class="metric">
                  <span class="mk">命中笔数</span>
                  <span class="mv">{{ replayReport.n_hits }}</span>
                </div>
                <div class="metric">
                  <span class="mk">胜率</span>
                  <span class="mv" :class="replayReport.win_rate >= 0.5 ? 'up' : 'down'">
                    {{ (replayReport.win_rate * 100).toFixed(1) }}%
                  </span>
                </div>
                <div class="metric">
                  <span class="mk">平均盈亏比</span>
                  <span class="mv">{{ replayReport.avg_rr.toFixed(2) }}</span>
                </div>
                <div class="metric">
                  <span class="mk">最大回撤</span>
                  <span class="mv down">{{ replayReport.max_drawdown.toFixed(2) }}</span>
                </div>
                <div class="metric">
                  <span class="mk">年化收益</span>
                  <span class="mv" :class="replayReport.annualized_return >= 0 ? 'up' : 'down'">
                    {{ (replayReport.annualized_return * 100).toFixed(2) }}%
                  </span>
                </div>
                <div class="metric">
                  <span class="mk">平均持仓</span>
                  <span class="mv">{{ replayReport.avg_holding_bars.toFixed(1) }} 日</span>
                </div>
              </div>

              <div class="report-section">
                <div class="block-title">资金曲线（年化收益）</div>
                <v-chart
                  v-if="replayReport.equity_curve.length"
                  class="equity-chart" :option="equityChartOption" autoresize
                />
                <span v-else class="hint">无资金曲线数据（命中 0 笔）</span>
              </div>

              <div class="report-section">
                <div class="block-title">买卖流水（{{ replayReport.trades.length }} 笔）</div>
                <el-table :data="replayReport.trades" size="small" max-height="320" empty-text="无成交">
                  <el-table-column prop="symbol" label="标的" width="100" />
                  <el-table-column label="形态" width="90">
                    <template #default="{ row }">
                      <el-tag size="small" :type="patternTagType(row.pattern_type)">{{ patternLabel(row.pattern_type) }}</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="买入" width="160">
                    <template #default="{ row }">{{ row.entry_price.toFixed(2) }} @ {{ row.entry_date.slice(0, 10) }}</template>
                  </el-table-column>
                  <el-table-column label="卖出" width="160">
                    <template #default="{ row }">{{ row.exit_price.toFixed(2) }} @ {{ row.exit_date.slice(0, 10) }}</template>
                  </el-table-column>
                  <el-table-column prop="exit_reason" label="离场" width="100" />
                  <el-table-column label="盈亏比" width="80">
                    <template #default="{ row }">
                      <span :class="row.rr >= 0 ? 'up' : 'down'">{{ row.rr.toFixed(2) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="holding_bars" label="持仓(日)" width="80" />
                </el-table>
              </div>

              <div class="report-section">
                <div class="block-title">形态分布</div>
                <div class="dist-row">
                  <el-tag
                    v-for="[pattern, count] in patternDistEntries"
                    :key="pattern"
                    :type="patternTagType(pattern)"
                    size="small"
                  >{{ patternLabel(pattern) }}：{{ count }}</el-tag>
                  <span v-if="patternDistEntries.length === 0" class="hint">无命中</span>
                </div>
              </div>

              <div class="report-section">
                <div class="block-title">月度收益（累计盈亏比）</div>
                <div class="monthly-bars">
                  <div
                    v-for="item in sortedMonthlyReturns" :key="item.month"
                    class="monthly-bar"
                  >
                    <div
                      class="bar"
                      :class="item.rr >= 0 ? 'up' : 'down'"
                      :style="{ height: Math.min(Math.abs(item.rr) * 40, 60) + 'px' }"
                      :title="`${item.month}: ${item.rr.toFixed(2)}`"
                    ></div>
                    <span class="bar-month">{{ item.month.slice(5) }}</span>
                  </div>
                  <span v-if="sortedMonthlyReturns.length === 0" class="hint">无月度数据</span>
                </div>
              </div>

              <div class="report-section">
                <div class="block-title">生产建议</div>
                <div class="recommendation">{{ replayReport.min_rr_ratio_recommendation }}</div>
              </div>
            </div>
            <div v-else class="empty-block">
              <span class="hint">运行回放后展示胜率/盈亏比/年化曲线/买卖流水（默认 cfg 零命中时，展开上方「策略参数」放宽阈值）</span>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </section>
  </div>
</template>

<style scoped>
/* 根壳：纵向 flex（顶条 + 主体左右分栏 + 底部 tab） */
.caisen-shell {
  padding: var(--qt-space-3);
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--qt-space-3);
  background: var(--qt-bg-page);
  overflow: hidden;
}

/* 顶部标题条 */
.top-bar {
  display: flex;
  align-items: center;
  gap: var(--qt-space-3);
  padding: var(--qt-space-2) var(--qt-space-3);
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: var(--qt-radius);
  flex-shrink: 0;
}
.top-bar .title {
  font-size: 14px;
  font-weight: 700;
  color: var(--qt-text-primary);
}
.top-bar .subtitle {
  font-size: var(--qt-fs-caption);
  color: var(--qt-text-secondary);
  margin-right: auto;
}

/* 主体：左右分栏（列表 38% + 图表 62%） */
.main-area {
  flex: 1;
  display: flex;
  gap: var(--qt-space-3);
  min-height: 0;                              /* flex 子项防溢出关键 */
}

/* 左栏：候选列表 */
.plans-card {
  width: 38%;
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: var(--qt-radius);
  padding: var(--qt-space-2);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.plans-card :deep(.el-table) { flex: 1; overflow: auto; }

/* 右栏：图表 */
.chart-card {
  flex: 1;
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: var(--qt-radius);
  padding: var(--qt-space-2);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.chart-title {
  font-size: var(--qt-fs-title);
  color: var(--qt-text-primary);
  margin-bottom: var(--qt-space-2);
  display: flex;
  align-items: center;
  gap: var(--qt-space-2);
}
.chart-title .hint { color: var(--qt-text-secondary); font-weight: normal; }
.chart-title .warn-text { color: var(--qt-warn); font-size: var(--qt-fs-caption); }

/* K 线画布容器：lightweight-charts 接管，autoSize 自适应父容器 */
.chart-container {
  flex: 1;
  min-height: 280px;
  width: 100%;
}

/* 选中计划关键参数面板（图表下方） */
.plan-detail {
  margin-top: var(--qt-space-2);
  padding: var(--qt-space-2);
  background: var(--qt-bg-elevated);
  border-radius: var(--qt-radius-sm);
  flex-shrink: 0;
}
.detail-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--qt-space-3);
  font-size: var(--qt-fs-caption);
  line-height: 1.8;
}
.dk { color: var(--qt-text-secondary); margin-right: 4px; }
.dv {
  color: var(--qt-text-primary);
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.dv.up { color: var(--qt-up); }
.dv.down { color: var(--qt-down); }

/* 底部 tab 区 */
.bottom-card {
  flex-shrink: 0;
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: var(--qt-radius);
  padding: var(--qt-space-2) var(--qt-space-3);
  max-height: 38%;
  overflow: auto;
}
.bottom-tabs :deep(.el-tabs__content) { padding-top: var(--qt-space-1); }

.bottom-grid {
  display: flex;
  gap: var(--qt-space-4);
  flex-wrap: wrap;
}
.form-block {
  background: var(--qt-bg-elevated);
  border-radius: var(--qt-radius-sm);
  padding: var(--qt-space-2) var(--qt-space-3);
  flex: 1;
  min-width: 320px;
}
.block-title {
  font-size: var(--qt-fs-title);
  color: var(--qt-text-primary);
  margin-bottom: var(--qt-space-2);
  font-weight: 600;
}
.empty-block {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 120px;
  color: var(--qt-text-secondary);
  font-size: var(--qt-fs-caption);
}
.hint { font-size: var(--qt-fs-caption); color: var(--qt-text-secondary); }
.rr-value {
  color: var(--qt-accent);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.loss-text { color: var(--qt-down); font-variant-numeric: tabular-nums; }
.mono { font-family: var(--qt-font-mono); font-size: var(--qt-fs-caption); }

/* 回放报告区 */
.replay-area { display: flex; flex-direction: column; gap: var(--qt-space-3); }
.replay-report { display: flex; flex-direction: column; gap: var(--qt-space-3); }
.report-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: var(--qt-space-2);
}
.metric {
  display: flex;
  flex-direction: column;
  padding: var(--qt-space-2) var(--qt-space-3);
  background: var(--qt-bg-elevated);
  border-radius: var(--qt-radius-sm);
}
.mk { font-size: var(--qt-fs-caption); color: var(--qt-text-secondary); }
.mv {
  font-size: 18px;
  font-weight: 700;
  color: var(--qt-text-primary);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.mv.up { color: var(--qt-up); }
.mv.down { color: var(--qt-down); }

.report-section {
  padding: var(--qt-space-2) var(--qt-space-3);
  background: var(--qt-bg-elevated);
  border-radius: var(--qt-radius-sm);
}
.dist-row { display: flex; gap: var(--qt-space-2); flex-wrap: wrap; }

/* 月度收益柱状（简易 div 实现，避免引入 ECharts 增重） */
.monthly-bars {
  display: flex;
  gap: 6px;
  align-items: flex-end;
  height: 80px;
  margin-top: var(--qt-space-2);
  overflow-x: auto;
}
.monthly-bar {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  min-width: 28px;
}
.bar {
  width: 16px;
  border-radius: 2px 2px 0 0;
}
.bar.up { background: var(--qt-up); }
.bar.down { background: var(--qt-down); }
.bar-month {
  font-size: 10px;
  color: var(--qt-text-secondary);
  font-family: var(--qt-font-mono);
}

.recommendation {
  font-size: var(--qt-fs-body);
  color: var(--qt-text-regular);
  line-height: 1.6;
  margin-top: var(--qt-space-1);
}

/* 回测跑通批次：年化收益曲线 + 策略参数表单 */
.equity-chart {
  width: 100%;
  height: 260px;
}
.cfg-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--qt-space-2);
}
.cfg-item {
  display: flex;
  align-items: center;
  gap: var(--qt-space-2);
}
.cfg-label {
  font-size: var(--qt-fs-caption);
  color: var(--qt-text-secondary);
  min-width: 130px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
