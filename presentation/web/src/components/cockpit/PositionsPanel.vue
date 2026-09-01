<!--
  PositionsPanel 持仓面板（可视化重构 2026-09-01：cockpit 补洞——此前全站
  没有持仓组件，/gm/positions 端点一直无人消费）。

  物理意图：持仓表 + 行内盈亏条（正负双色、按最大绝对值归一）+ 点击行开
  K 线回放抽屉（KlinePanel 消费 ohlcv 快照；无快照标的显示占位不猜数据）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>持仓 · {{ leg === 'exp' ? '实验腿' : '主腿' }}（{{ rows.length }} 只）</span>
        <span class="sub">点击行看 K 线形态回放</span>
      </div>
    </template>
    <el-table :data="rows" size="small" v-loading="loading" @row-click="open"
              :empty-text="loading ? '加载中…' : '空仓'">
      <el-table-column label="标的" min-width="150">
        <template #default="{ row }">
          <span class="sym">{{ toTs(row.symbol) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="volume" label="数量" width="90" />
      <el-table-column label="成本" width="90">
        <template #default="{ row }">{{ fmt(row.vwap) }}</template>
      </el-table-column>
      <el-table-column label="浮动盈亏" min-width="200">
        <template #default="{ row }">
          <div class="pnl-cell">
            <span :class="row.fpnl >= 0 ? 'up' : 'down'">
              {{ row.fpnl >= 0 ? '+' : '' }}{{ Math.round(row.fpnl || 0) }}
            </span>
            <div class="bar-track">
              <div class="bar" :class="row.fpnl >= 0 ? 'up' : 'down'"
                   :style="{ width: barWidth(row.fpnl) }"></div>
            </div>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="drawer" size="62%" :title="drawerTitle" :with-header="true">
      <div v-if="kline" class="drawer-body">
        <div class="kline-meta">
          <span v-if="kline.marks?.rr">RR {{ kline.marks.rr }}</span>
          <span v-if="kline.marks?.formed_at">形态日 {{ kline.marks.formed_at }}</span>
          <span class="sub">数据截至 {{ kline.asof }} · 前复权</span>
        </div>
        <KlinePanel :data="kline" />
      </div>
      <el-empty v-else-if="drawer" description="该标的无 K 线快照（非持仓/当日信号标的不预生成）" />
    </el-drawer>
  </el-card>
</template>

<script setup lang="ts">
import { computed, inject, onMounted, ref, watch, type Ref } from 'vue'
import { getPositions, type GmPositionRow } from '../../api/gm'
import { getOhlcv, type OhlcvData } from '../../api/home'
import KlinePanel from '../charts/KlinePanel.vue'

const leg = inject<Ref<string>>('cockpit-leg', ref('main'))
const loading = ref(false)
const rows = ref<GmPositionRow[]>([])
const drawer = ref(false)
const drawerTitle = ref('')
const kline = ref<OhlcvData | null>(null)

/** gm 符号 SZSE.300433 → ts 口径 300433.SZ（ohlcv 快照键、展示统一 ts）。 */
function toTs(sym?: string): string {
  const [ex, code] = String(sym || '.').split('.')
  const sfx = { SHSE: 'SH', SZSE: 'SZ' }[ex] || ex
  return `${code}.${sfx}`
}
const fmt = (v?: number) => (v != null && Number.isFinite(v) ? v.toFixed(2) : '—')

const maxAbs = computed(() => Math.max(1, ...rows.value.map((r) => Math.abs(r.fpnl || 0))))
const barWidth = (fpnl?: number) =>
  `${Math.min(100, (Math.abs(fpnl || 0) / maxAbs.value) * 100)}%`

async function load() {
  loading.value = true
  try {
    rows.value = await getPositions(leg.value)
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}

async function open(row: GmPositionRow) {
  const ts = toTs(row.symbol)
  drawerTitle.value = `${ts} K 线形态回放`
  drawer.value = true
  kline.value = await getOhlcv(ts)
}

watch(leg, load)
onMounted(load)
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
.sym { font-family: var(--el-font-family-mono, monospace); cursor: pointer; }
.pnl-cell { display: flex; align-items: center; gap: 8px; }
.bar-track { flex: 1; height: 8px; background: #1b222d; border-radius: 4px; overflow: hidden; }
.bar { height: 100%; border-radius: 4px; }
.bar.up, .up { color: #ef5350; background: #ef5350; }
.bar.down, .down { color: #26a69a; background: #26a69a; }
.bar.up { background: linear-gradient(90deg, #ef535055, #ef5350); }
.bar.down { background: linear-gradient(90deg, #26a69a55, #26a69a); }
.drawer-body { padding: 0 8px; }
.kline-meta { display: flex; gap: 16px; margin-bottom: 8px; color: var(--el-text-color-primary); font-size: 13px; }
</style>
