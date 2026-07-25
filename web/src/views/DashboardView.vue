<script setup lang="ts">
/**
 * 板块驾驶舱（路由 /dashboard）
 *
 * 定位：板块轮动前端可视化的主视图，呈现「板块资金流 → 活跃股池」两层信息，
 * 供研究员快速判断板块轮动方向。
 *
 * 两块布局：
 *   ① 板块资金流横向条形（融资余额增速 Top，sector/flow 端点）
 *   ② 活跃股池表（sector/flow 端点 pool 字段，下期接入湖后填充）
 *
 * 离线降级红线（贯穿全视图）：
 *   后端在无 sector 湖时返空结构（sectors:[] / pool:[]），本视图所有面板均做
 *   空态兜底（TerminalWatermark 极简水印或图表空 option），绝不白屏。原因：
 *   板块湖依赖离线同步脚本（sync_sector_daily），开发机/CI 默认无数据，前端
 *   必须能渲染空骨架供联调，避免「无数据 = 整页崩」。
 *
 * 数据加载策略：
 *   onMounted 拉 sector/flow 端点（单端点，无并发必要，但保留 async 便于扩展）。
 *
 * 历史：原 regime/credit 两块面板已随 CreditRegime 与对应后端端点（/macro/regime、
 * /macro/credit）于 2026-07 整体下线删除；前端旧版「四块布局」同步收缩为
 * 「两块布局」。factors 端点亦于 T7 架构治理批 2 删除（前端无调用方）。
 */
import { ref, computed, onMounted, markRaw } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { BarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import TerminalWatermark from '../components/TerminalWatermark.vue'
import {
  getSectorFlow,
  type SectorFlowResponse,
} from '../api/macro'

// 按需注册 ECharts：柱状（板块）+ 一组 component + Canvas 渲染器。
// 不全量引入 echarts，控制 bundle 体积。
use([
  BarChart,
  TitleComponent, TooltipComponent, GridComponent, LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
])

// ============ 响应式状态 ============

/** 板块资金流 + 活跃股池（sector/flow 端点） */
const sectorData = ref<SectorFlowResponse>({ sectors: [], pool: [] })
/** 全局加载态（端点落定前为 true） */
const loading = ref(true)

// ============ 数据加载 ============

async function loadDashboard() {
  loading.value = true
  // 单端点：rejected 时保持空态兜底（拦截器已弹 ElMessage，这里静默吸收避免重复提示）
  try {
    const data = await getSectorFlow()
    sectorData.value = markRaw(data)
  } catch {
    // 保持空态兜底，不抛错
  }
  loading.value = false
}

onMounted(loadDashboard)

// ============ ① 板块资金流横向条形 ============

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

// ============ ② 活跃股池表 ============

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
        <h1>板块驾驶舱</h1>
        <span class="dash-sub">板块资金流 / 活跃股池</span>
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

    <!-- 主体：CSS Grid 1×2 两块面板（板块条形 + 活跃股池） -->
    <main class="dash-grid">
      <!-- ① 板块资金流横向条形 -->
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

      <!-- ② 活跃股池表 -->
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
 *   外的剩余高度。两块面板在窄屏下总高可能超出，故本视图自身允许纵向滚动
 *   （overflow:auto），把滚动限制在驾驶舱内部，不污染整页。
 */
.dashboard-shell {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--qt-bg-page);
  color: var(--qt-text-primary);
  display: flex;
  flex-direction: column;
}

/* 顶部工具条：固定高度，细分隔线分隔主体 */
.dash-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid var(--qt-border);
  background: var(--qt-bg-card);
  flex-shrink: 0;
}

.dash-title h1 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--qt-text-primary);
}

.dash-sub {
  font-size: 11px;
  color: var(--qt-text-secondary);
  margin-left: 8px;
}

/* 主体 Grid：1×2 两块，每块独立卡片；min-height 让 2 块在宽屏下也能撑满 */
.dash-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: minmax(280px, 1fr);
  gap: 10px;
  padding: 10px;
}

/* 每个面板单元格：暗卡片 + 极弱灰边框 + 内边距 + 隐藏溢出（图表自适应填充） */
.cell {
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: 6px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cell-caption {
  font-size: 12px;
  color: var(--qt-text-secondary);
  margin-bottom: 8px;
  flex-shrink: 0;
}

/* 图表/表格撑满单元格 */
.fill-chart {
  flex: 1;
  min-height: 200px;
}

.cell-sector { grid-column: 1 / 2; grid-row: 1 / 2; }
.cell-pool   { grid-column: 2 / 3; grid-row: 1 / 2; }
</style>
