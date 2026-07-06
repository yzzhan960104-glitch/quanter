<script setup lang="ts">
/**
 * 因子全生命周期视图（层级二·路由 /factors）
 *
 * 一级页面（看板）：按 status 三态分组（实盘服役 / 训练调研 / 已退役），
 *   每组 FactorMatrixCard 矩阵，点击卡片打开 drill-down drawer。
 * 二级页面（drill-down drawer）：元数据 + 关联数据集 + 引用策略 + IC/IR 衰减分析。
 *   - IC 衰减折线：horizon → ic_mean / ic_ir（预测力随持有期衰减曲线）
 *   - 月度×horizon 热力图：IC 在时间×持有期二维上的分布（衰减热力图）
 *   非面板因子（grid_computable=false）→ 展示「不支持 IC 衰减」告警，不报错。
 *
 * 数据流：getFactors → 分组 → openDetail(f) → getFactorDetail + getFactorICDecay
 *        → markRaw 写 chart option → drawer 内渲染。
 *
 * 反黑盒：因子清单、状态、IC 计算全部来自后端，前端只做反射与图表编排。
 */
import { ref, computed, onMounted, markRaw } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { HeatmapChart, LineChart } from 'echarts/charts'
import {
  TooltipComponent, GridComponent, VisualMapComponent, LegendComponent,
} from 'echarts/components'
import {
  getFactors, getFactorDetail, getFactorICDecay,
  type FactorSummary, type FactorDetail, type ICDecayResult,
} from '@/api/factors'
import FactorMatrixCard from '@/components/FactorMatrixCard.vue'
import { logger } from '@/utils/logger'

use([CanvasRenderer, HeatmapChart, LineChart, TooltipComponent, GridComponent, VisualMapComponent, LegendComponent])

const factors = ref<FactorSummary[]>([])
const loading = ref(false)
// drill-down drawer 状态
const drawerOpen = ref(false)
const selected = ref<FactorSummary | null>(null)
const detail = ref<FactorDetail | null>(null)
const decay = ref<ICDecayResult | null>(null)
const decayLoading = ref(false)
// IC 评估区间（drill-down 用；默认近 1 年）
const dateRange = ref<[string, string]>(defaultLastYear())

function defaultLastYear(): [string, string] {
  const end = new Date(); const start = new Date()
  start.setFullYear(start.getFullYear() - 1)
  return [fmt(start), fmt(end)]
}
function fmt(d: Date): string { return d.toISOString().slice(0, 10) }

/** 按 status 三态分组（前端矩阵分类展示） */
const groups = computed(() => [
  { key: 'live', label: '实盘服役', color: '#26a69a', items: factors.value.filter(f => f.status === 'live') },
  { key: 'training', label: '训练/调研', color: '#d29922', items: factors.value.filter(f => f.status === 'training') },
  { key: 'deprecated', label: '已退役', color: '#787b86', items: factors.value.filter(f => f.status === 'deprecated') },
])

async function fetchFactors() {
  loading.value = true
  try {
    factors.value = await getFactors()
  } catch (e: any) {
    logger.error('因子注册表拉取失败:', e)
  } finally {
    loading.value = false
  }
}

/** 打开 drill-down：拉 detail + （面板因子）IC 衰减 */
async function openDetail(f: FactorSummary) {
  selected.value = f
  drawerOpen.value = true
  detail.value = null
  decay.value = null
  try {
    detail.value = await getFactorDetail(f.name)
  } catch (e: any) {
    logger.error('因子详情拉取失败:', e)
  }
  if (f.grid_computable) {
    decayLoading.value = true
    try {
      decay.value = await getFactorICDecay(f.name, {
        start: dateRange.value[0], end: dateRange.value[1],
      })
    } catch (e: any) {
      decay.value = { ok: false, name: f.name, label: f.label, reason: 'IC 衰减计算失败：' + (e?.message || '') }
    } finally {
      decayLoading.value = false
    }
  } else {
    // 非面板因子：前端预置原因（与后端语义一致），不发请求
    decay.value = {
      ok: false, name: f.name, label: f.label,
      reason: `因子为 ${f.input_kind} 型，非横截面面板因子，不支持 IC 衰减分析`,
    }
  }
}

/** 重新计算 IC 衰减（用户改了区间后点「重算」） */
async function recomputeDecay() {
  if (!selected.value || !selected.value.grid_computable) return
  decayLoading.value = true
  decay.value = null
  try {
    decay.value = await getFactorICDecay(selected.value.name, {
      start: dateRange.value[0], end: dateRange.value[1],
    })
  } catch (e: any) {
    ElMessage.error('IC 衰减重算失败：' + (e?.message || ''))
  } finally {
    decayLoading.value = false
  }
}

/** IC 衰减折线：horizon → ic_mean / ic_ir 双轴 */
const decayLineOption = computed(() => {
  const d = decay.value
  if (!d || !d.ok || !d.decay || !d.decay.length) return null
  const horizons = d.decay.map(p => `${p.horizon}日`)
  return markRaw({
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: ['IC均值', 'ICIR'], top: 0 },
    grid: { left: 50, right: 50, top: 36, bottom: 30 },
    xAxis: { type: 'category', data: horizons, name: '持有期' },
    yAxis: [
      { type: 'value', name: 'IC均值', position: 'left', splitLine: { lineStyle: { color: '#2b3139' } } },
      { type: 'value', name: 'ICIR', position: 'right', splitLine: { show: false } },
    ],
    series: [
      {
        name: 'IC均值', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
        data: d.decay.map(p => Number(p.ic_mean.toFixed(4))),
        lineStyle: { width: 2, color: '#2962ff' }, itemStyle: { color: '#2962ff' },
      },
      {
        name: 'ICIR', type: 'line', yAxisIndex: 1, smooth: true, symbol: 'circle', symbolSize: 6,
        data: d.decay.map(p => Number(p.ic_ir.toFixed(3))),
        lineStyle: { width: 2, color: '#d29922' }, itemStyle: { color: '#d29922' },
      },
    ],
  })
})

/** 月度 × horizon IC 热力图：红(neg)→中性→绿(pos) */
const heatmapOption = computed(() => {
  const d = decay.value
  if (!d || !d.ok || !d.heatmap || !d.heatmap.data.length) return null
  const hm = d.heatmap
  return markRaw({
    animation: false,
    tooltip: {
      formatter: (p: any) => {
        const m = hm.months[p.data[0]]; const h = hm.horizons[p.data[1]]
        return `${m}<br/>持有 ${h} 日<br/>IC = <b>${Number(p.data[2]).toFixed(4)}</b>`
      },
    },
    grid: { left: 70, right: 24, top: 16, bottom: 60 },
    xAxis: {
      type: 'category', data: hm.horizons.map(h => `${h}日`), name: '持有期',
      splitArea: { show: true }, axisLabel: { color: '#b2b5be' },
    },
    yAxis: {
      type: 'category', data: hm.months, splitArea: { show: true },
      axisLabel: { color: '#b2b5be', fontSize: 10 },
    },
    visualMap: {
      min: -0.1, max: 0.1, calculable: true, orient: 'horizontal',
      left: 'center', bottom: 4, textStyle: { color: '#787b86' },
      // 红(负IC·反向预测) → 中性灰 → 绿(正IC·正向预测)
      inRange: { color: ['#ef5350', '#3a4049', '#d29922', '#26a69a'] },
    },
    series: [{
      type: 'heatmap', data: hm.data,
      label: { show: false },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.5)' } },
    }],
  })
})

onMounted(fetchFactors)
</script>

<template>
  <div class="fm-view">
    <div class="page-header">
      <div class="title">因子全生命周期</div>
      <div class="sub">反射 @register_factor 注册表 · 按 status 分组 · 点击卡片查看 IC 衰减与引用策略</div>
      <el-button size="small" :loading="loading" @click="fetchFactors">刷新</el-button>
    </div>

    <!-- 三态分组看板 -->
    <div class="groups">
      <section v-for="g in groups" :key="g.key" class="group" v-show="g.items.length">
        <div class="group-title">
          <span class="dot" :style="{ background: g.color }" />
          {{ g.label }}
          <span class="count">{{ g.items.length }}</span>
        </div>
        <div class="card-grid">
          <FactorMatrixCard
            v-for="f in g.items" :key="f.name" :factor="f"
            @click="openDetail"
          />
        </div>
      </section>
      <div v-if="!factors.length && !loading" class="empty">因子注册表为空（后端未扫描到 @register_factor 因子）</div>
    </div>

    <!-- drill-down drawer -->
    <el-drawer v-model="drawerOpen" size="52%" :title="selected?.label || '因子详情'" direction="rtl">
      <template v-if="selected">
        <div class="drawer-body">
          <!-- 元数据头 -->
          <div class="meta">
            <div class="meta-row">
              <el-tag size="small" effect="dark" :type="selected.status === 'live' ? 'success' : selected.status === 'deprecated' ? 'info' : 'warning'">
                {{ selected.status === 'live' ? '实盘' : selected.status === 'deprecated' ? '退役' : '训练' }}
              </el-tag>
              <el-tag size="small" effect="plain">{{ selected.category }}</el-tag>
              <span class="mono">{{ selected.name }}</span>
            </div>
            <div v-if="selected.description" class="desc">{{ selected.description }}</div>

            <!-- 关联数据集 -->
            <div class="block">
              <div class="block-title">关联数据集</div>
              <div class="tags">
                <el-tag v-if="!detail?.datasets?.length" size="small" type="info">—</el-tag>
                <el-tag v-for="ds in detail?.datasets || []" :key="ds" size="small">{{ ds }}</el-tag>
              </div>
            </div>

            <!-- 引用策略 -->
            <div class="block">
              <div class="block-title">被策略引用（{{ detail?.referenced_by?.length || 0 }}）</div>
              <div class="tags">
                <el-tag v-if="!detail?.referenced_by?.length" size="small" type="info">暂无（待策略 composition 接入）</el-tag>
                <el-tag v-for="s in detail?.referenced_by || []" :key="s.name" size="small" effect="plain">{{ s.label }}</el-tag>
              </div>
            </div>
          </div>

          <!-- IC 衰减区 -->
          <div class="decay-section">
            <div class="block-title">
              IC/IR 衰减分析
              <span v-if="decay?.ok && decay.n_symbols" class="muted">（{{ decay.n_symbols }} 只标的）</span>
            </div>
            <div v-if="selected.grid_computable" class="decay-toolbar">
              <el-date-picker
                v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" size="small"
                start-placeholder="开始" end-placeholder="结束" style="width: 240px"
              />
              <el-button size="small" type="primary" plain :loading="decayLoading" @click="recomputeDecay">重算</el-button>
            </div>

            <div v-if="decayLoading" v-loading="true" class="chart-placeholder" />
            <div v-else-if="decay && !decay.ok" class="alert">
              {{ decay.reason || '不支持 IC 衰减分析' }}
            </div>
            <template v-else>
              <div v-if="decayLineOption" class="chart-block">
                <div class="chart-subtitle">IC 衰减曲线（持有期 → 预测力）</div>
                <v-chart class="chart" :option="decayLineOption" autoresize theme="terminal-dark" />
              </div>
              <div v-if="heatmapOption" class="chart-block">
                <div class="chart-subtitle">月度 × 持有期 IC 热力图</div>
                <v-chart class="chart heatmap" :option="heatmapOption" autoresize theme="terminal-dark" />
              </div>
            </template>
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.fm-view { flex: 1; overflow: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 12px; }
.page-header { display: flex; align-items: baseline; gap: 12px; }
.page-header .title { font-size: 15px; font-weight: 700; color: #d1d4dc; }
.page-header .sub { font-size: 11px; color: #787b86; flex: 1; }

.groups { display: flex; flex-direction: column; gap: 14px; }
.group { background: transparent; }
.group-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: #b2b5be; margin-bottom: 8px; font-weight: 600;
}
.group-title .dot { width: 8px; height: 8px; border-radius: 50%; }
.group-title .count { color: #787b86; font-weight: 400; }
.card-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px;
}
.empty { color: #787b86; padding: 32px; text-align: center; }

/* drawer 内容 */
.drawer-body { padding: 0 16px 24px; display: flex; flex-direction: column; gap: 14px; }
.meta { display: flex; flex-direction: column; gap: 8px; }
.meta-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.meta-row .mono { font-size: 11px; color: #787b86; font-family: ui-monospace, Menlo, monospace; }
.desc { font-size: 12px; color: #b2b5be; line-height: 1.6; }
.block { margin-top: 4px; }
.block-title { font-size: 12px; color: #d1d4dc; font-weight: 600; margin-bottom: 6px; display: flex; align-items: baseline; gap: 6px; }
.block-title .muted { font-size: 10px; color: #787b86; font-weight: 400; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; }

.decay-section { border-top: 1px solid #2b3139; padding-top: 12px; }
.decay-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.chart-block { background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px; margin-bottom: 10px; }
.chart-subtitle { font-size: 12px; color: #b2b5be; margin-bottom: 4px; }
.chart { height: 240px; }
.chart.heatmap { height: 320px; }
.chart-placeholder { height: 200px; }
.alert {
  font-size: 12px; color: #d29922; background: rgba(210, 153, 34, 0.1);
  border: 1px solid rgba(210, 153, 34, 0.3); border-radius: 4px; padding: 10px;
}
</style>
