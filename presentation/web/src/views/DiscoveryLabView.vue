<script setup lang="ts">
/**
 * 搜索实验室 DiscoveryLabView（P3 · spec §4 · 2026-08-13）
 *
 * 三块布局（全部只读）：
 *   ① 敏感性仪表板——21 维边际效应 + 主效应排名 + 死参数徽标 + 覆盖盲区警告
 *      （trial 语料 = Sobol 均匀覆盖 = 天然 DOE，数据源 /research/discovery/sensitivity）
 *   ② 热力图——任意两维 × 目标指标网格（n_obs 同行防「单点热区」误导）
 *   ③ 搜索进展——trial 数 / 最新 run / 新冠军（复用既有 discovery/status）
 *
 * 空态兜底：语料为空/端点降级 → 空表 + 提示，绝不白屏。
 */
import { ref, computed, onMounted, markRaw } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { BarChart, HeatmapChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import {
  getSensitivity,
  getHeatmap,
  getParams,
  getDiscoveryStatus,
  type SensitivityResponse,
  type HeatmapResponse,
  type ParamSpaceItem,
  type DiscoveryStatus,
} from '@/api/discovery'

// 按需注册 ECharts：柱状（边际效应）+ 热力图（两维网格）——不全量引入，控制 bundle。
use([
  BarChart,
  HeatmapChart,
  TitleComponent, TooltipComponent, GridComponent, LegendComponent,
  VisualMapComponent,
  CanvasRenderer,
])

// ============ 响应式状态 ============
const sens = ref<SensitivityResponse | null>(null)
const heatmap = ref<HeatmapResponse | null>(null)
const params = ref<ParamSpaceItem[]>([])
const constraints = ref<string[]>([])
const status = ref<DiscoveryStatus | null>(null)
const loading = ref(true)
/** 热力图维度选择（x/y/metric），默认窗口 × 止损 ATR 倍数的 calmar 网格 */
const hx = ref('window')
const hy = ref('stop_atr_mult')
const metric = ref('calmar')

// ============ 数据加载 ============
async function loadAll() {
  loading.value = true
  try {
    const [s, p, st] = await Promise.all([getSensitivity(), getParams(), getDiscoveryStatus()])
    sens.value = s
    params.value = p.param_space
    constraints.value = p.constraints
    status.value = st
  } catch {
    // 端点降级：保持空态渲染（client.ts 拦截器已 Toast 错误）
  }
  await loadHeatmap()
  loading.value = false
}

async function loadHeatmap() {
  if (!hx.value || !hy.value) return
  try {
    heatmap.value = await getHeatmap(hx.value, hy.value, metric.value)
  } catch {
    heatmap.value = null
  }
}

/** 维度选择器变更 → 重取热力图 */
function onHeatmapDimsChange() {
  void loadHeatmap()
}

// ============ 图表 option ============
/** 主效应排名条形图（spread 降序，死参数档红色标注） */
const rankingChartOption = computed<Record<string, unknown>>(() => {
  const s = sens.value
  if (!s || s.ranking.length === 0) return {}
  return markRaw({
    grid: { left: 130, right: 30, top: 10, bottom: 30 },
    xAxis: { type: 'value', name: 'calmar 档间极差' },
    yAxis: { type: 'category', data: s.ranking.map((r) => r.key).reverse() },
    series: [{
      type: 'bar',
      data: s.ranking.map((r) => ({
        value: r.spread,
        itemStyle: { color: s.dead_params.includes(r.key) ? '#f56c6c' : '#409eff' },
      })).reverse(),
    }],
    tooltip: { trigger: 'axis' },
  })
})

/** 热力图 option：x/y 档网格 + 样本量角标（无样本格透明） */
const heatmapChartOption = computed<Record<string, unknown>>(() => {
  const h = heatmap.value
  if (!h || h.x_axis.length === 0 || h.y_axis.length === 0) return {}
  const data: [number, number, number][] = []
  h.grid.forEach((row, yi) => {
    row.forEach((v, xi) => {
      if (v !== null) data.push([xi, yi, v])
    })
  })
  return markRaw({
    grid: { left: 90, right: 90, top: 10, bottom: 60 },
    xAxis: { type: 'category', data: h.x_axis, name: hx.value },
    yAxis: { type: 'category', data: h.y_axis, name: hy.value },
    visualMap: { min: Math.min(...data.map((d) => d[2])), max: Math.max(...data.map((d) => d[2])),
      orient: 'horizontal', left: 'center', bottom: 0 },
    series: [{
      type: 'heatmap',
      data,
      label: { show: true, formatter: (p: { data: [number, number, number] }) => {
        // n_obs 角标：网格值下方标注样本量，防「单点热区」误导（spec §4.2）
        const n = h.n_obs[p.data[1]]?.[p.data[0]] ?? 0
        return `${p.data[2]}\n(n=${n})`
      } },
    }],
    tooltip: { trigger: 'item' },
  })
})

/** 死参数表格行（徽标数据源） */
const deadRows = computed(() => {
  const s = sens.value
  if (!s) return []
  return s.ranking
    .filter((r) => s.dead_params.includes(r.key))
    .map((r) => ({ key: r.key, flag: '死参数（档间极差≈0，可降维）' }))
})

/** 盲区文本列表 */
const blindList = computed(() => {
  const s = sens.value
  if (!s) return ''
  return Object.entries(s.blind_spots)
    .map(([k, lv]) => `${k}: ${lv.join('/')}`)
    .join('；')
})

onMounted(() => {
  void loadAll()
})
</script>

<template>
  <div class="qt-page">
    <h2>搜索实验室</h2>
    <p class="qt-sub">
      参数发现 trial 语料敏感性分析（数据源：discovery_trials.db · 只读）。
      主效应极差大 = 调它显著改变 calmar；死参数 = 极差≈0 的档（可降维）；盲区 = 候选档从未被采样。
    </p>

    <div v-if="loading" class="qt-hint">加载中…</div>
    <template v-else>
      <!-- ① 敏感性仪表板 -->
      <div class="qt-card lab-row-top">
        <div class="lab-pane">
          <h3>主效应排名（inner calmar 档间极差）</h3>
          <VChart v-if="rankingChartOption.series" :option="rankingChartOption"
                  autoresize style="height: 420px" />
          <div v-else class="qt-hint">语料不足（n_trials={{ sens?.n_trials ?? 0 }}），暂无可分析维度</div>
        </div>
        <div class="lab-pane">
          <h3>死参数候选 + 覆盖盲区</h3>
          <el-table :data="deadRows" size="small" border>
            <el-table-column prop="key" label="参数" width="160" />
            <el-table-column prop="flag" label="标记" />
          </el-table>
          <p class="qt-sub">
            死参数（极差 ≤ 全局最大极差×10%）：{{ sens?.dead_params.join('、') || '无' }}
          </p>
          <p class="qt-sub" v-if="blindList.length > 0">
            盲区：{{ blindList }}
          </p>
          <p class="qt-sub" v-else>覆盖盲区：无（所有候选档均有样本）</p>
        </div>
      </div>

      <!-- ② 热力图 -->
      <div class="qt-card lab-heatmap">
        <h3>两维热力图（样本量角标防单点热区）</h3>
        <div class="lab-selects">
          <span>X 维</span>
          <el-select v-model="hx" @change="onHeatmapDimsChange" size="small" style="width: 180px">
            <el-option v-for="p in params" :key="p.key" :label="p.key" :value="p.key" />
          </el-select>
          <span>Y 维</span>
          <el-select v-model="hy" @change="onHeatmapDimsChange" size="small" style="width: 180px">
            <el-option v-for="p in params" :key="p.key" :label="p.key" :value="p.key" />
          </el-select>
        </div>
        <VChart v-if="heatmapChartOption.series" :option="heatmapChartOption"
                autoresize style="height: 380px" />
        <div v-else class="qt-hint">所选维度无样本交集——换一对维度试试</div>
      </div>

      <!-- ③ 搜索进展 -->
      <div class="qt-card lab-status">
        <h3>搜索进展</h3>
        <p class="qt-sub">
          trial 数：{{ status?.n_trials ?? '—' }} ·
          最新 run：{{ status?.latest_run?.run_id ?? '—' }} ·
          新冠军 trial：{{ status?.champion?.trial_id ?? '—' }}
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.qt-page { padding: var(--qt-space-3); }
.qt-sub { color: var(--qt-text-dim, #888); font-size: 13px; margin: 4px 0 12px; }
.qt-hint { color: var(--qt-text-dim, #888); padding: 24px 0; text-align: center; }
.lab-row-top { display: grid; grid-template-columns: 1fr 1fr; gap: var(--qt-space-2); }
.lab-pane { padding: var(--qt-space-2); }
.lab-heatmap, .lab-status { margin-top: var(--qt-space-2); padding: var(--qt-space-2); }
.lab-selects { display: flex; gap: var(--qt-space-2); align-items: center; margin-bottom: 8px; }
</style>
