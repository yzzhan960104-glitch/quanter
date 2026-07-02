<script setup lang="ts">
/**
 * 宏观·板块驾驶舱（路由 /dashboard）
 *
 * 定位：宏观 CTA 前端可视化的主视图，全景式呈现「宏观信贷状态 → 三因子流动性 →
 * 板块资金流 → 活跃股池」四层信息，供研究员快速判断 risk-on/off 与板块轮动方向。
 *
 * 四块布局：
 *   ① 顶部状态卡 + 信贷状态历史色带 timeline（regime 端点）
 *   ② 信贷三因子折线（社融 / M1M2_gap / DR007，credit 端点）
 *   ③ 板块资金流横向条形（融资余额增速 Top，sector/flow 端点）
 *   ④ 活跃股池表（sector/flow 端点 pool 字段，下期接入湖后填充）
 *
 * 离线降级红线（贯穿全视图）：
 *   后端在无 macro/sector 湖时返空结构（series:{} / sectors:[] / pool:[]），
 *   本视图所有面板均做空态兜底（TerminalWatermark 极简水印或图表空 option），
 *   绝不白屏。原因：宏观湖依赖离线同步脚本（sync_macro_daily），开发机/CI 默认
 *   无数据，前端必须能渲染空骨架供联调，避免「无数据 = 整页崩」。
 *
 * 数据加载策略：
 *   onMounted 并发拉三个端点（Promise.allSettled，单个失败不阻塞其余）。
 *   Why allSettled 而非 all：三个端点独立，credit 失败不应连累 regime 卡片
 *   渲染；allSettled 让每个 promise 各自落定，前端按状态分流渲染。
 */
import { ref, computed, onMounted, markRaw } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart, BarChart, HeatmapChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
  VisualMapComponent,
  SingleAxisComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import TerminalWatermark from '../components/TerminalWatermark.vue'
import {
  getMacroRegime,
  getMacroCredit,
  getSectorFlow,
  type MacroRegimeResponse,
  type MacroCreditResponse,
  type SectorFlowResponse,
  type RegimeValue,
} from '../api/macro'

// 按需注册 ECharts：折线（三因子）/ 柱状（板块）/ 热力（regime 色带）
// + 一组 component + Canvas 渲染器。不全量引入 echarts，控制 bundle 体积。
use([
  LineChart, BarChart, HeatmapChart,
  TitleComponent, TooltipComponent, GridComponent, LegendComponent,
  DataZoomComponent, VisualMapComponent, SingleAxisComponent,
  CanvasRenderer,
])

// ============ 响应式状态 ============

/** 宏观信贷状态（regime 端点）；null = 未加载或失败 */
const regimeData = ref<MacroRegimeResponse | null>(null)
/** 信贷三因子时序（credit 端点）；空对象 = 未加载或降级 */
const creditData = ref<MacroCreditResponse>({ series: {} })
/** 板块资金流 + 活跃股池（sector/flow 端点） */
const sectorData = ref<SectorFlowResponse>({ sectors: [], pool: [] })
/** 全局加载态（三个端点全部落定前为 true） */
const loading = ref(true)

// ============ 数据加载 ============

async function loadDashboard() {
  loading.value = true
  // Promise.allSettled：单端点失败不连累其它面板渲染（参见上方注释）
  const [regimeP, creditP, sectorP] = await Promise.allSettled([
    getMacroRegime(),
    getMacroCredit(),
    getSectorFlow(),
  ])
  // 按落定状态分流写入；rejected 的端点保持空态兜底（不抛错，不弹第二个 Toast
  // ——拦截器已对单个 reject 弹过 ElMessage，这里静默吸收避免重复提示）
  if (regimeP.status === 'fulfilled') regimeData.value = markRaw(regimeP.value)
  if (creditP.status === 'fulfilled') creditData.value = markRaw(creditP.value)
  if (sectorP.status === 'fulfilled') sectorData.value = markRaw(sectorP.value)
  loading.value = false
}

onMounted(loadDashboard)

// ============ ① 信贷状态卡 ============

/** 当前信贷状态文本 + 配色映射（regime 三态穷尽分支） */
const regimeDisplay = computed(() => {
  const r = regimeData.value?.regime
  switch (r) {
    case 1: return { label: '扩张 (Risk-On)', desc: '信用宽松，倾向加仓高 beta', color: '#26a69a', bg: '#0d2818' }
    case -1: return { label: '收缩 (Risk-Off)', desc: '紧信用环境，防御为主', color: '#ef5350', bg: '#2d1014' }
    case 0: return { label: '中性', desc: '震荡市，仓位中性', color: '#d29922', bg: '#2d2410' }
    default: return { label: '数据不足', desc: '等待宏观湖同步', color: '#787b86', bg: '#1e222d' }
  }
})

// ============ ② 信贷状态历史色带 timeline（heatmap） ============

/**
 * regime 历史序列 → heatmap data
 *
 * Why 用 heatmap 而非折线/柱状：regime 是离散三态（+1/0/-1），连续折线会误导
 * 出现中间值；heatmap 用颜色块表达每日离散状态，红/黄/绿三色直观看迁移节奏。
 * 单轴（singleAxis）+ heatmap 是 ECharts 表达离散日历色带的标准组合。
 */
const regimeTimelineOption = computed(() => {
  const history = regimeData.value?.history ?? []
  // heatmap data 形态：[x_index, y_index(固定0), value]
  const data = history.map((p, i) => [i, 0, p.regime])
  return markRaw({
    tooltip: {
      formatter: (params: { data: [number, number, RegimeValue]; name?: string }) => {
        const [i, , v] = params.data
        const date = history[i]?.date ?? ''
        const tag = v === 1 ? '扩张' : v === -1 ? '收缩' : '中性'
        return `${date}<br/>状态：${tag}`
      },
    },
    grid: { left: 0, right: 0, top: 10, bottom: 0, containLabel: false },
    xAxis: { type: 'category', show: false, data: history.map((p) => p.date) },
    yAxis: { type: 'category', show: false, data: ['regime'] },
    visualMap: {
      min: -1, max: 1,
      show: false,
      inRange: { color: ['#ef5350', '#d29922', '#26a69a'] }, // 红(收缩)/黄(中性)/绿(扩张)
    },
    series: [
      {
        type: 'heatmap',
        data,
        // 大格子高度，让色带视觉上更显眼；边框色对齐极夜黑底
        itemStyle: { borderColor: '#131722', borderWidth: 1 },
      },
    ],
  })
})

/** timeline 是否有数据（无数据时不渲染空 heatmap） */
const hasRegimeHistory = computed(() => (regimeData.value?.history ?? []).length > 0)

// ============ ③ 信贷三因子折线 ============

/**
 * credit series → 多折线 option
 *
 * 三因子：社融（信贷脉冲）/ M1M2_gap（货币活化）/ DR007（银行间流动性）。
 * 三者量纲不同，必须双 Y 轴——左轴社融/M1M2_gap（百分比类），右轴 DR007（利率）。
 *
 * 列名约定（与后端 macro 湖列名对齐，灵活 pick，缺列优雅跳过）：
 *   社融 → social_financing / shibor / 含 '社融' 关键字的列
 *   M1M2 → m1m2_gap / 含 'm1m2' 关键字
 *   DR007 → dr007 / 含 'dr007' 关键字
 */
const creditChartOption = computed(() => {
  const series = creditData.value.series ?? {}
  // 按 key 关键字模糊匹配三因子（容错：后端列名可能带后缀/大小写差异）
  const pick = (keys: string[]) => {
    for (const k of Object.keys(series)) {
      const lower = k.toLowerCase()
      if (keys.some((kw) => lower.includes(kw))) return { name: k, points: series[k] }
    }
    return null
  }
  const social = pick(['social', '社融', 'shibor'])
  const m1m2 = pick(['m1m2', 'm1_m2'])
  const dr007 = pick(['dr007', 'dr_007'])

  // 合并三因子的日期并集，作为 x 轴（保证三条线对齐到统一时间轴）
  const dateSet = new Set<string>()
  for (const f of [social, m1m2, dr007]) {
    if (f) for (const p of f.points) dateSet.add(p.date)
  }
  const dates = Array.from(dateSet).sort()

  // 把每条序列转为按日期索引的 Map，便于 O(1) 对齐
  const toMap = (points?: { date: string; value: number }[]) => {
    const m = new Map<string, number>()
    if (points) for (const p of points) m.set(p.date, p.value)
    return m
  }
  const socialM = toMap(social?.points)
  const m1m2M = toMap(m1m2?.points)
  const dr007M = toMap(dr007?.points)

  const buildSeries = (name: string, m: Map<string, number>, yAxisIdx: number, color: string) => ({
    name,
    type: 'line' as const,
    yAxisIndex: yAxisIdx,
    data: dates.map((d) => (m.has(d) ? m.get(d) : null)),
    smooth: true,
    symbol: 'none',
    lineStyle: { width: 1.6, color },
    itemStyle: { color },
    // 缺值断线（停牌/未公布日），不插值——避免前视污染
    connectNulls: false,
  })

  const seriesArr: unknown[] = []
  if (social) seriesArr.push(buildSeries(`社融(${social.name})`, socialM, 0, '#2962ff'))
  if (m1m2) seriesArr.push(buildSeries(`M1M2差(${m1m2.name})`, m1m2M, 0, '#26a69a'))
  if (dr007) seriesArr.push(buildSeries(`DR007(${dr007.name})`, dr007M, 1, '#ef5350'))

  return markRaw({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    legend: { top: 0, textStyle: { color: '#d1d4dc', fontSize: 11 } },
    grid: { left: 50, right: 50, top: 36, bottom: 50 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLine: { lineStyle: { color: '#2b3139' } },
      axisLabel: { color: '#787b86', fontSize: 10 },
    },
    yAxis: [
      {
        type: 'value',
        name: '社融/M1M2',
        position: 'left',
        axisLine: { lineStyle: { color: '#2b3139' } },
        axisLabel: { color: '#787b86', fontSize: 10 },
        splitLine: { lineStyle: { color: '#232731' } },
        nameTextStyle: { color: '#787b86', fontSize: 10 },
      },
      {
        type: 'value',
        name: 'DR007',
        position: 'right',
        axisLine: { lineStyle: { color: '#2b3139' } },
        axisLabel: { color: '#787b86', fontSize: 10 },
        splitLine: { show: false },
        nameTextStyle: { color: '#787b86', fontSize: 10 },
      },
    ],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
    series: seriesArr,
  })
})

/** 三因子折线是否有数据（任一序列非空即视为有） */
const hasCreditSeries = computed(() => {
  const s = creditData.value.series ?? {}
  return Object.values(s).some((arr) => arr && arr.length > 0)
})

// ============ ④ 板块资金流横向条形 ============

/**
 * 板块融资余额增速 Top 条形（sector/flow 端点 sectors 字段）。
 *
 * 后端 sectors 是 to_dict('records') 直出，字段名随 sector 湖 schema 而定。
 * 这里按常见字段名优先 pick 板块名 + 增速/净流入值，缺字段则降级到首列/次列。
 * Top 3 高亮：data 中前 3 项 itemStyle 单独染红，视觉锚定龙头板块。
 */
const sectorChartOption = computed(() => {
  const sectors = sectorData.value.sectors ?? []
  if (sectors.length === 0) return null

  // 板块名字段候选（按优先级）
  const nameKey = ['sector_name', '板块', 'name', '板块名']
    .find((k) => sectors[0] && k in (sectors[0] as object)) as string | undefined
  // 数值字段候选（按优先级：增速类 > 净流入类）
  const valKey = ['margin_growth', '融资余额增速', 'growth', 'net_inflow', '净流入', '主力净流入']
    .find((k) => sectors[0] && k in (sectors[0] as object)) as string | undefined

  // 降级：找不到约定字段时，取 records 第二个字段当 value（第一个当 name）
  const keys = sectors[0] ? Object.keys(sectors[0] as object) : []
  const finalNameKey = nameKey ?? keys[0] ?? ''
  const finalValKey = valKey ?? keys[1] ?? ''

  // 取前 12 条（条形图过多会拥挤），并按绝对值降序让 Top3 自然排在前
  const rows = sectors.slice(0, 12).map((rec) => {
    const v = Number(rec[finalValKey] ?? 0)
    return { name: String(rec[finalNameKey] ?? '—'), value: isFinite(v) ? v : 0 }
  })

  // 条形图 y 轴从下往上画，需反转使 Top1 在最上方
  const sorted = [...rows].sort((a, b) => a.value - b.value)
  const topCount = 3 // Top 3 高亮
  // 排序后末尾 topCount 个是龙头（值最大），高亮红色
  const highlightNames = new Set(sorted.slice(-topCount).map((r) => r.name))

  return markRaw({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
    },
    grid: { left: 10, right: 30, top: 10, bottom: 10, containLabel: true },
    xAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: '#2b3139' } },
      axisLabel: { color: '#787b86', fontSize: 10 },
      splitLine: { lineStyle: { color: '#232731' } },
    },
    yAxis: {
      type: 'category',
      data: sorted.map((r) => r.name),
      axisLine: { lineStyle: { color: '#2b3139' } },
      axisLabel: { color: '#d1d4dc', fontSize: 11 },
    },
    series: [
      {
        type: 'bar',
        data: sorted.map((r) => ({
          value: r.value,
          // 龙头板块高亮红，其余默认 Quant 蓝；why 不用 visualMap：单维离散高亮
          // 用 itemStyle 直接染更直观
          itemStyle: { color: highlightNames.has(r.name) ? '#ef5350' : '#2962ff' },
        })),
        barWidth: '60%',
        label: {
          show: true,
          position: 'right',
          color: '#787b86',
          fontSize: 10,
          formatter: (p: { value: number }) => p.value.toFixed(2),
        },
      },
    ],
  })
})

const hasSector = computed(() => (sectorData.value.sectors ?? []).length > 0)

// ============ ⑤ 活跃股池表 ============

/** 活跃股池（当前后端 pool 占位返 []，下期接入湖后填充；前端按字段就绪渲染） */
const poolRows = computed(() => {
  const pool = sectorData.value.pool ?? []
  // pool 当前是 string[]（股票代码）；转成表格行结构，预留换手率/动量字段位
  return pool.map((code) => ({ code, turnover: '—', momentum: '—' }))
})
</script>

<template>
  <!-- 驾驶舱外壳：暗黑底色 + 顶部刷新条 + 滚动主体（与终端不同，本页可纵向滚动） -->
  <div class="dashboard-shell">
    <!-- 顶部工具条：标题 + 刷新按钮 + 加载态 -->
    <header class="dash-header">
      <div class="dash-title">
        <h1>宏观 · 板块驾驶舱</h1>
        <span class="dash-sub">CreditRegime / 三因子流动性 / 板块资金流</span>
      </div>
      <el-button
        size="small"
        type="primary"
        plain
        :loading="loading"
        @click="loadDashboard"
      >
        刷新快照
      </el-button>
    </header>

    <!-- 主体：CSS Grid 2×2 四块面板 -->
    <main class="dash-grid">
      <!-- ① 状态卡 + 历史色带 timeline -->
      <section class="cell cell-regime">
        <div class="regime-card" :style="{ background: regimeDisplay.bg, borderColor: regimeDisplay.color }">
          <div class="regime-label" :style="{ color: regimeDisplay.color }">
            {{ regimeDisplay.label }}
          </div>
          <div class="regime-desc">{{ regimeDisplay.desc }}</div>
        </div>
        <div class="timeline-wrap">
          <div class="cell-caption">近 60 日信贷状态迁移色带</div>
          <v-chart
            v-if="hasRegimeHistory"
            class="timeline-chart"
            :option="regimeTimelineOption"
            autoresize
          />
          <!-- 空态：极简水印替代 el-empty，紧凑模式适配色带窄面板 -->
          <TerminalWatermark
            v-else
            compact
            subtitle="暂无 regime 历史数据"
          />
        </div>
      </section>

      <!-- ② 信贷三因子折线 -->
      <section class="cell cell-credit">
        <div class="cell-caption">信贷三因子（社融 / M1M2差 / DR007）</div>
        <v-chart
          v-if="hasCreditSeries"
          class="fill-chart"
          :option="creditChartOption"
          autoresize
        />
        <TerminalWatermark
          v-else
          compact
          subtitle="暂无 credit 时序（macro 湖未同步）"
        />
      </section>

      <!-- ③ 板块资金流条形 -->
      <section class="cell cell-sector">
        <div class="cell-caption">板块融资余额增速 Top（红色 = Top3 龙头）</div>
        <v-chart
          v-if="sectorChartOption"
          class="fill-chart"
          :option="sectorChartOption"
          autoresize
        />
        <TerminalWatermark
          v-else
          compact
          subtitle="暂无板块资金流（sector 湖未同步）"
        />
      </section>

      <!-- ④ 活跃股池表 -->
      <section class="cell cell-pool">
        <div class="cell-caption">活跃股池（换手率 / 动量）</div>
        <el-table
          v-if="poolRows.length > 0"
          :data="poolRows"
          size="small"
          stripe
          height="100%"
          style="width: 100%"
        >
          <el-table-column prop="code" label="代码" min-width="100" />
          <el-table-column prop="turnover" label="换手率" width="90" align="right" />
          <el-table-column prop="momentum" label="动量" width="90" align="right" />
        </el-table>
        <TerminalWatermark
          v-else
          compact
          subtitle="活跃股池待接入（pool 湖下期填充）"
        />
      </section>
    </main>
  </div>
</template>

<style scoped>
/*
 * 驾驶舱外壳：与终端共享极夜黑底色。
 *
 * Why flex:1 + min-height:0 + overflow:auto 而非 min-height:100vh：
 *   App.vue 是「顶部导航(36px) + router-view」纵向 flex 壳，本视图填满除导航
 *   外的剩余高度。4 块面板在窄屏下总高可能超出，故本视图自身允许纵向滚动
 *   （overflow:auto），把滚动限制在驾驶舱内部，不污染整页。
 */
.dashboard-shell {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: #131722;
  color: #d1d4dc;
  display: flex;
  flex-direction: column;
}

/* 顶部工具条：固定高度，细分隔线分隔主体 */
.dash-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid #2b3139;
  background: #1e222d;
  flex-shrink: 0;
}

.dash-title h1 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: #d1d4dc;
}

.dash-sub {
  font-size: 11px;
  color: #787b86;
  margin-left: 8px;
}

/* 主体 Grid：2×2 四块，每块独立卡片；min-height 让 4 块在宽屏下也能撑满 */
.dash-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: minmax(280px, 1fr) minmax(280px, 1fr);
  gap: 10px;
  padding: 10px;
}

/* 每个面板单元格：暗卡片 + 极弱灰边框 + 内边距 + 隐藏溢出（图表自适应填充） */
.cell {
  background: #1e222d;
  border: 1px solid #2b3139;
  border-radius: 6px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cell-caption {
  font-size: 12px;
  color: #787b86;
  margin-bottom: 8px;
  flex-shrink: 0;
}

/* ① 状态卡 + 色带：左大号状态卡 + 右色带 timeline */
.cell-regime {
  grid-column: 1 / 2;
  grid-row: 1 / 2;
  flex-direction: row;
  align-items: stretch;
  gap: 10px;
}

.regime-card {
  flex: 0 0 200px;
  border: 1px solid;
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.regime-label {
  font-size: 20px;
  font-weight: 700;
  margin-bottom: 6px;
}

.regime-desc {
  font-size: 12px;
  color: #787b86;
  line-height: 1.5;
}

.timeline-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0; /* 让 flex 子项可收缩，避免 ECharts 撑爆 */
}

.timeline-chart {
  flex: 1;
  min-height: 80px;
}

/* 三因子 / 板块 / 池表：图表/表格撑满单元格 */
.fill-chart {
  flex: 1;
  min-height: 200px;
}

.cell-credit { grid-column: 2 / 3; grid-row: 1 / 2; }
.cell-sector { grid-column: 1 / 2; grid-row: 2 / 3; }
.cell-pool   { grid-column: 2 / 3; grid-row: 2 / 3; }
</style>
