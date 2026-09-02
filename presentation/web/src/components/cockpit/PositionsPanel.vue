<!--
  PositionsPanel 持仓面板（可视化重构 2026-09-01；同日用户需求③加富化列）。

  物理意图：持仓表=作战名单——公司名/数量/成本/现价/盈亏双色条 + 目标价族
  （止损/TP1/TP2，ohlcv 快照 marks 合并，缺快照标的该列 '—' 不猜）。
  点击行开 K 线回放抽屉（KlinePanel 消费同一份快照）。
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
          <span class="name">{{ rich(row.symbol)?.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="进场日" width="90">
        <template #default="{ row }">{{ rich(row.symbol)?.marks?.entry_date?.slice(5) || '—' }}</template>
      </el-table-column>
      <el-table-column label="预计超期" width="90">
        <template #default="{ row }">
          <span :class="expireCls(row.symbol)">
            {{ rich(row.symbol)?.marks?.expire_date?.slice(5) || '—' }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="volume" label="数量" width="80" />
      <el-table-column label="成本" width="76">
        <template #default="{ row }">{{ fmt(row.vwap) }}</template>
      </el-table-column>
      <el-table-column label="现价" width="76">
        <template #default="{ row }">
          <span :class="pnlClass(row)">{{ last(row.symbol) ?? '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="浮动盈亏" min-width="150">
        <template #default="{ row }">
          <div class="pnl-cell">
            <span :class="pnlClass(row)">
              {{ (row.fpnl ?? 0) >= 0 ? '+' : '' }}{{ Math.round(row.fpnl || 0) }}
            </span>
            <div class="bar-track">
              <div class="bar" :class="(row.fpnl ?? 0) >= 0 ? 'up' : 'down'"
                   :style="{ width: barWidth(row.fpnl) }"></div>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="止损" width="70">
        <template #default="{ row }">{{ m(row.symbol, 'stop') }}</template>
      </el-table-column>
      <el-table-column label="TP1" width="70">
        <template #default="{ row }">{{ m(row.symbol, 'tp1_price') }}</template>
      </el-table-column>
      <el-table-column label="TP2" width="70">
        <template #default="{ row }">{{ m(row.symbol, 'tp2_price') }}</template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="drawer" size="62%" :title="drawerTitle" :with-header="true">
      <div v-if="kline" class="drawer-body">
        <div class="kline-meta">
          <span v-if="kline.marks?.rr">RR {{ kline.marks.rr }}</span>
          <span v-if="kline.marks?.formed_at">形态日 {{ kline.marks.formed_at }}</span>
          <span v-if="kline.marks?.signal_entry">委托 {{ kline.marks.signal_entry.toFixed(2) }}</span>
          <span v-if="kline.marks?.entry_price">成本 {{ kline.marks.entry_price.toFixed(2) }}</span>
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
/** ts 符号 → ohlcv 快照（名称/现价/目标价的富化源；无快照标的优雅缺省）。 */
const enrich = ref<Record<string, OhlcvData | null>>({})

/** gm 符号 SZSE.300433 → ts 口径 300433.SZ（ohlcv 快照键、展示统一 ts）。 */
function toTs(sym?: string): string {
  const [ex, code] = String(sym || '.').split('.')
  const sfx: Record<string, string> = { SHSE: 'SH', SZSE: 'SZ' }
  return `${code}.${sfx[ex] || ex}`
}
const fmt = (v?: number | null) =>
  (v != null && Number.isFinite(v) ? v.toFixed(2) : '—')
const rich = (sym?: string) => enrich.value[toTs(sym)]
const last = (sym?: string): string | null => {
  const d = rich(sym)
  return d?.rows?.length ? String(d.rows[d.rows.length - 1][3]) : null
}
const m = (sym: string | undefined, key: 'stop' | 'tp1_price' | 'tp2_price'): string =>
  fmt(rich(sym)?.marks?.[key])
const pnlClass = (row: GmPositionRow) => ((row.fpnl ?? 0) >= 0 ? 'up' : 'down')
/** 预计超期着色：距超期 ≤3 交易日 → 橙警示；已越线 → 红（策略次日尾盘将强平）。 */
const expireCls = (sym?: string): string => {
  const m = rich(sym)?.marks
  if (!m?.expire_date) return ''
  const days = m.max_holding ?? 30
  const held = m.entry_date
    ? Math.round((Date.now() - new Date(m.entry_date).getTime()) / 86400000) : 0
  // 自然日近似（展示口径）：剩余 <25% 或已越线即警示
  if (held >= days * 1.45) return 'expired'
  if (held >= days * 1.45 - 5) return 'expiring'
  return ''
}

const maxAbs = computed(() => Math.max(1, ...rows.value.map((r) => Math.abs(r.fpnl || 0))))
const barWidth = (fpnl?: number) =>
  `${Math.min(100, (Math.abs(fpnl ?? 0) / maxAbs.value) * 100)}%`

async function load() {
  loading.value = true
  try {
    rows.value = await getPositions(leg.value)
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
  // 富化（N 个小快照并行拉取；缺文件 → null 占位，列显 '—'）
  enrich.value = {}
  await Promise.all(rows.value.map(async (r) => {
    enrich.value[toTs(r.symbol)] = await getOhlcv(toTs(r.symbol)).catch(() => null)
  }))
}

async function open(row: GmPositionRow) {
  const ts = toTs(row.symbol)
  const name = enrich.value[ts]?.name
  drawerTitle.value = `${ts}${name ? ` ${name}` : ''} · K 线形态回放`
  drawer.value = true
  kline.value = enrich.value[ts]
}

watch(leg, load)
onMounted(load)
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
.sym { font-family: var(--qt-font-mono, monospace); cursor: pointer; }
.name { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 8px; }
.pnl-cell { display: flex; align-items: center; gap: 8px; }
.bar-track { flex: 1; height: 8px; background: var(--qt-bg-overlay, #f0f2f5);
             border-radius: 4px; overflow: hidden; }
.bar { height: 100%; border-radius: 4px; }
.bar.up { background: linear-gradient(90deg, #ef535055, #ef5350); }
.bar.down { background: linear-gradient(90deg, #26a69a55, #26a69a); }
.up { color: #ef5350; }
.expiring { color: var(--qt-warn, #b88230); font-weight: 600; }
.expired { color: #ef5350; font-weight: 700; }
.down { color: #26a69a; }
.drawer-body { padding: 0 8px; }
.kline-meta { display: flex; gap: 16px; margin-bottom: 8px;
              color: var(--el-text-color-primary); font-size: 13px; }
</style>
