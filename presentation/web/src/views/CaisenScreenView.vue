<script setup lang="ts">
/**
 * 蔡森形态学只读观测大屏（Phase 1 · 前端只读化；Spec 2 Task 8 退役回放 tab）。
 *
 * 物理定位：本视图退化为**纯观测**——不持任何写能力。三栏布局（复用 --qt-* token +
 * Element Plus，与 LiveCockpitView 同构）：
 *   ① 左栏：候选计划列表（ElTable，按 rr_ratio 降序，徽章 pattern_type/status）
 *   ② 右栏：lightweight-charts K 线图（candles + markers 形态点 + priceLines 止损止盈/颈线）
 *   ③ 底部：只读观测提示（指向 EOD/veto_plan.py/pre_open cron 三处真实写入口）
 *
 * 写职责真实归属（Phase 1 · 前端只读化后）：
 *   - 扫描筛选：候选计划由 EOD 事件链自动产出（不再前端触发扫描）；
 *   - 审核否决：走 veto_plan.py（不再前端 approve/reject）；
 *   - 激活挂单：由 pre_open cron（09:22）自动执行（不再前端 activate）。
 *
 * 顶部操作动线（纯观测）：刷新列表（listPlans）→ 选中 → 看 K 线（getChart）→ 看关键参数。
 *
 * Spec 2 Task 8 退役记录：原「历史回放」tab（走同步 runReplay API）已下线，回测能力
 * 全部迁至独立路由 /lab（ParamLabView，走异步任务 + 抽屉式新建）。
 *
 * 红线（CLAUDE.md 量化风控·边界审查）：
 *   - lightweight-charts 实例 onBeforeUnmount 必销毁（防 canvas 内存泄漏）；
 *   - 切换选中计划时先 remove 旧 priceLines 再画新（防残留虚线堆叠）；
 *   - markers 经 createSeriesMarkers（v5 推荐入口，setMarkers v6 将移除）；
 *   - 状态完全跟随后端返回值，前端不本地推断（杜绝"虚假繁荣"）。
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
// ECharts 资金曲线随回放结果渲染迁入 ReplayReportPanel（Spec 2 Task 4），/lab 消费；
// Spec 2 Task 8 下线 /caisen 老「历史回放」tab 后，本视图不再直接注册 ECharts、也不复用该面板。
import {
  listPlans, getChart,
  type CandidatePlan, type ChartData,
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
const loadingChart = ref(false)
// Phase 1 · 前端只读化：撤除 scanning/reviewing/activating + scanForm/editForm + canActivate/canReview。
// 候选计划由 EOD 事件链自动产出，审核否决走 veto_plan.py，激活挂单由 pre_open cron（09:22）自动执行；
// 本视图退化为纯观测——listPlans 拉候选列表 + getChart 渲染 K 线 + 关键参数面板。

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

// ============ 选中计划联动：加载 chart（只读观测，无 edits 同步） ============
watch(selectedPlan, async (plan) => {
  if (!plan) {
    chartData.value = null
    return
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

// ============ 操作：刷新候选列表（只读视图仅此一项主动操作） ============
// Phase 1 · 前端只读化：onScan/onReview/onActivate 三个写操作函数已撤除——
// 扫描由 EOD 事件链自动产出候选，审核否决走 veto_plan.py，激活挂单由 pre_open cron 自动执行。
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

// ============ 生命周期 ============
onMounted(async () => {
  initChart()
  await refreshPlans()
  // Spec 2 Task 8：回放 tab 退役后，策略参数 schema（configSchema）与回测历史
  // 列表（loadReplayRuns）的初载随之移除——两者均为回放链路专属。/lab 路由独立
  // 持有这些能力（异步任务 + 抽屉式新建），本视图回归纯审核职责。
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

// 回放链路（月度收益/形态分布/资金曲线 ECharts option、策略参数反射表单、回测历史表）已随
// Spec 2 Task 8 老「历史回放」tab 下线一并移除——回测能力由 /lab 独立路由承接。
// Phase 1 · 前端只读化：canActivate/canReview 两个 computed 撤除（写操作已全无，依赖同步消失）。
</script>

<template>
  <div class="caisen-shell">
    <!-- 顶部标题条 -->
    <div class="top-bar">
      <span class="title">蔡森形态学 · T 日候选审核</span>
      <span class="subtitle">候选列表（EOD 自动产出）→ 选中 → 看图（只读观测）</span>
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
          :data="sortedPlans" size="small" empty-text="暂无候选（候选计划由 EOD 事件链自动产出）"
          highlight-current-row
          @current-change="onSelectPlan"
          max-height="100%"
        >
          <el-table-column label="标的" width="140">
            <template #default="{ row }">
              <span :title="row.symbol">{{ row.symbol_name || row.symbol }}</span>
            </template>
          </el-table-column>
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

    <!-- 底部：只读观测提示（Phase 1 · 前端只读化：撤除扫描参数 + 审核操作两块表单） -->
    <section class="bottom-card">
      <div class="readonly-hint">
        蔡森候选计划由 EOD 事件链自动产出；审核否决请赴 <code>veto_plan.py</code>；激活挂单由 pre_open cron（09:22）自动执行。本视图仅作只读观测。
      </div>
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

/* 底部只读提示卡（Phase 1 · 前端只读化：撤除扫描参数 + 审核操作两块表单后代之以只读提示） */
.bottom-card {
  flex-shrink: 0;
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: var(--qt-radius);
  padding: var(--qt-space-2) var(--qt-space-3);
  max-height: 38%;
  overflow: auto;
}
.readonly-hint {
  font-size: 12px;
  color: var(--qt-text-secondary);
  line-height: 1.7;
  padding: 8px 4px;
}
.readonly-hint code {
  color: var(--qt-accent);
  font-family: var(--qt-font-mono);
}
.hint { font-size: var(--qt-fs-caption); color: var(--qt-text-secondary); }
.rr-value {
  color: var(--qt-accent);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.loss-text { color: var(--qt-down); font-variant-numeric: tabular-nums; }
.mono { font-family: var(--qt-font-mono); font-size: var(--qt-fs-caption); }
</style>
