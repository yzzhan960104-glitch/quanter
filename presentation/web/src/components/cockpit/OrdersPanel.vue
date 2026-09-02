<!--
  OrdersPanel 当日委托全景（全读可视化 P5.1 · 2026-09-03）。

  物理意图：流水卡「成交|委托」Tab 的委托半场——当日柜台 orders 全状态：
  时间（UTC→本地）/标的+名/方向/价格/数量/已成交/状态 tag/委托号。
  拒单行（status=8）tooltip 展示 ordRejReason（柜台拒因票据，如科创板
  200 股门槛）；数据=gm_orders_{leg}.json 快照（在线态走 /gm/orders），
  零管道新增。纯内容组件（无卡壳）——宿主是 TradesTable 的 Tab pane。
-->
<template>
  <div>
    <el-table :data="rows" size="small" height="320" v-loading="loading"
              :empty-text="loading ? '加载中…' : '当日无委托'">
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
      <el-table-column label="已成交" width="110">
        <template #default="{ row }">
          <span class="mono">{{ row.filled_volume ?? 0 }}<span class="sub">/{{ row.volume }}</span></span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="88">
        <template #default="{ row }">
          <el-tooltip v-if="row.status === 8" :content="row.ordRejReason || '柜台未给拒因'"
                      placement="top">
            <el-tag type="danger" size="small">已拒 ⚠</el-tag>
          </el-tooltip>
          <el-tag v-else :type="statusOf(row).type" size="small">{{ statusOf(row).label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="委托号" min-width="110">
        <template #default="{ row }">
          <span class="oid">{{ String(row.cl_ord_id || row.clOrdId || '').slice(0, 8) }}</span>
        </template>
      </el-table-column>
    </el-table>
    <div class="note">status：3=成交 · 8=拒（悬停看拒因）· 5/6=撤 · 悬停 ⚠ 见柜台拒因票据</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, inject, watch, type Ref } from 'vue'
import { getOrders, type GmOrderRow } from '../../api/gm'
import { getOhlcv } from '../../api/home'

const props = defineProps<{ leg?: string }>()
// 显式 prop 优先；宿主未传时与 TradesTable 同吃 cockpit-leg 注入（零改动兼容）
const injected = inject<Ref<string>>('cockpit-leg', ref('main'))
const leg = computed(() => props.leg ?? injected.value)
const loading = ref(false)
const rows = ref<GmOrderRow[]>([])
const names = ref<Record<string, string>>({})

const toTs = (sym?: string): string => {
  const [ex, code] = String(sym || '.').split('.')
  const sfx: Record<string, string> = { SHSE: 'SH', SZSE: 'SZ' }
  return `${code}.${sfx[ex] || ex}`
}
const num = (v: unknown): string =>
  (typeof v === 'number' && Number.isFinite(v)) ? v.toFixed(2) : '—'
const timeOf = (iso?: string): string => {
  // 7002 created_at 是 UTC（Z 后缀）——转本地时区（与 TradesTable 同口径）
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? String(iso).slice(11, 19) || '—' : d.toLocaleTimeString('zh-CN', { hour12: false })
}

/** 状态 tag 映射（方案 §5.1：3=成交 8=拒 5/6=撤；有部分成交的中间态显部分）。 */
function statusOf(row: GmOrderRow): { label: string; type: 'success' | 'warning' | 'info' | 'primary' } {
  const filled = Number(row.filled_volume ?? 0)
  if (row.status === 3) return { label: '成交', type: 'success' }
  if (filled > 0) return { label: `部分 ${filled}`, type: 'warning' }
  if (row.status === 5 || row.status === 6) return { label: '已撤', type: 'info' }
  return { label: row.status != null ? `在途 ${row.status}` : '在途', type: 'primary' }
}

async function load() {
  loading.value = true
  try {
    rows.value = await getOrders(leg.value)
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
  const uniq = Array.from(new Set(rows.value.map((r) => toTs(r.symbol))))
  const got = await Promise.all(uniq.map(async (s) => {
    const d = await getOhlcv(s).catch(() => null)
    return [s, d?.name ?? ''] as const
  }))
  names.value = Object.fromEntries(got)
}

watch(leg, load)
onMounted(load)
</script>

<style scoped>
.sym { font-family: var(--qt-font-mono, monospace); }
.name { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 8px; }
.oid { font-family: var(--qt-font-mono, monospace);
       color: var(--el-text-color-secondary); font-size: 11px; }
.mono { font-family: var(--qt-font-mono, monospace); }
.mono .sub { color: var(--el-text-color-secondary); font-size: 11px; }
.note { color: var(--el-text-color-secondary); font-size: 11px; margin-top: 6px; }
</style>
