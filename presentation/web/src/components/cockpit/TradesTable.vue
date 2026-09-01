<!--
  TradesTable —— 交易流水表组件（一期观测运营层 · Task 9；2026-09-01 全字段升级）。

  物理意图：驾驶舱「交易流水」卡——当日成交全字段作战记录：时间/方向/标的+
  公司名/价格/数量/金额/委托号。方向 tag：买=红（A股买入警示色）/卖=绿。
  数据=GET /api/v1/gm/trades（在线）或 gm_trades_<leg>.json 静态快照；
  公司名经 ohlcv 快照富化（缺快照 '—' 不猜）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>交易流水 · {{ leg === 'exp' ? '实验腿' : '主腿' }}</span>
        <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </template>
    <el-table :data="page.trades" size="small" height="320" v-loading="loading">
      <el-table-column label="时间" width="84">
        <template #default="{ row }">{{ timeOf(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="方向" width="62">
        <template #default="{ row }">
          <el-tag :type="row.side === 1 ? 'danger' : 'success'" size="small" effect="plain">
            {{ row.side === 1 ? '买入' : '卖出' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="标的" min-width="150">
        <template #default="{ row }">
          <span class="sym">{{ toTs(row.symbol) }}</span>
          <span class="name">{{ names[toTs(row.symbol)] || '' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="价格" width="82">
        <template #default="{ row }">{{ num(row.price) }}</template>
      </el-table-column>
      <el-table-column prop="volume" label="数量" width="76" />
      <el-table-column label="金额" width="96">
        <template #default="{ row }">{{ num(row.amount, 0) }}</template>
      </el-table-column>
      <el-table-column label="委托号" min-width="110">
        <template #default="{ row }">
          <span class="oid">{{ String(row.clOrdId || row.cl_ord_id || '').slice(0, 8) }}</span>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-if="page.total > page.limit"
      layout="prev, pager, next"
      :total="page.total"
      :page-size="page.limit"
      :current-page="currentPage"
      @current-change="onPage"
      small
    />
  </el-card>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, inject, watch, type Ref } from 'vue'
import { getTrades, type GmTradeRow } from '../../api/gm'
import { getOhlcv } from '../../api/home'

const loading = ref(false)
const currentPage = ref(1)
const leg = inject<Ref<string>>('cockpit-leg', ref('main'))
const all = reactive<{ rows: GmTradeRow[] }>({ rows: [] })
const page = reactive<{ trades: GmTradeRow[]; total: number; limit: number }>({
  trades: [], total: 0, limit: 50,
})
/** ts 符号 → 公司名（ohlcv 快照富化；缺快照不显示——绝不猜名）。 */
const names = ref<Record<string, string>>({})

const toTs = (sym?: string): string => {
  const [ex, code] = String(sym || '.').split('.')
  const sfx: Record<string, string> = { SHSE: 'SH', SZSE: 'SZ' }
  return `${code}.${sfx[ex] || ex}`
}
const num = (v: unknown, nd = 2): string =>
  (typeof v === 'number' && Number.isFinite(v)) ? v.toFixed(nd) : '—'
const timeOf = (iso?: string): string => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? String(iso).slice(11, 19) || '—' : d.toTimeString().slice(0, 8)
}

async function load() {
  loading.value = true
  try {
    all.rows = await getTrades({ leg: leg.value })
    page.total = all.rows.length
    onPage(1)
  } finally {
    loading.value = false
  }
  const uniq = Array.from(new Set(all.rows.map((r) => toTs(r.symbol))))
  const got = await Promise.all(uniq.map(async (s) => {
    const d = await getOhlcv(s).catch(() => null)
    return [s, d?.name ?? ''] as const
  }))
  names.value = Object.fromEntries(got)
}

function onPage(p: number) {
  currentPage.value = p
  const start = (p - 1) * page.limit
  page.trades = all.rows.slice(start, start + page.limit)
}

watch(leg, load)
onMounted(load)
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sym { font-family: var(--qt-font-mono, monospace); }
.name { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 8px; }
.oid { font-family: var(--qt-font-mono, monospace);
       color: var(--el-text-color-secondary); font-size: 11px; }
</style>
