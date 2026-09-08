<!--
  DailyReviewView 每日回顾（2026-09-07 用户需求）：分账户四区块一页回顾。

  区块：① 当前持仓（PositionsPanel 复用，含止损/目标价富化与 K 线抽屉）
        ② 今日交易（gm_trades_history 按所选日期过滤；UTC→本地时区）
        ③ 亏损归因（LoserReviewCard 复用 + 委员会评审附签）
        ④ 明日计划（PlanCard 复用 + 委员会评审 + 均衡栈守卫预判）

  腿态：本视图 provide('cockpit-leg')——PositionsPanel/LoserReviewCard 等
  注入型组件跟随（LegSelector 的 provide 只达其自身子树，故选择器就地重写）。
  红线：全部只读快照/档案；委员会评审与 risk_state 均为观点标注非指令。
-->
<template>
  <div class="review-page">
    <div class="review-head">
      <div class="title">
        <h2>每日回顾</h2>
        <span class="sub">{{ day }}</span>
      </div>
      <div class="controls">
        <el-segmented v-if="legs.length > 1" v-model="leg" :options="legOptions" size="small" />
        <el-tag v-else-if="legs.length" size="small" effect="plain">{{ legs[0].label }}</el-tag>
        <el-date-picker v-model="day" type="date" size="small" value-format="YYYY-MM-DD"
                        :clearable="false" style="width: 140px" />
      </div>
    </div>

    <el-row :gutter="12">
      <el-col :span="24">
        <PositionsPanel />
      </el-col>
    </el-row>

    <el-row :gutter="12" class="mt">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div class="flex-between">
              <span>今日交易 <span class="sub">· {{ day }}</span></span>
              <span class="sub" v-if="dayTrades.length">
                {{ dayTrades.length }} 笔 · 买 {{ buyCount }} / 卖 {{ dayTrades.length - buyCount }}
              </span>
            </div>
          </template>
          <el-table :data="dayTrades" size="small" :empty-text="tradesLoading ? '加载中…' : '当日无成交'">
            <el-table-column label="时间" width="96">
              <template #default="{ row }">{{ localTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="标的" min-width="130">
              <template #default="{ row }">
                <span class="sym">{{ toTs(row.symbol) }}</span>
                <span v-if="names[toTs(row.symbol)]" class="name">{{ names[toTs(row.symbol)] }}</span>
              </template>
            </el-table-column>
            <el-table-column label="方向" width="64">
              <template #default="{ row }">
                <el-tag size="small" :type="row.side === 1 ? 'danger' : 'success'" effect="plain">
                  {{ row.side === 1 ? '买' : '卖' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="价格" width="86">
              <template #default="{ row }">{{ num(row.price) }}</template>
            </el-table-column>
            <el-table-column label="数量" width="80">
              <template #default="{ row }">{{ row.volume }}</template>
            </el-table-column>
            <el-table-column label="金额" width="110">
              <template #default="{ row }">{{ amountOf(row) }}</template>
            </el-table-column>
            <el-table-column label="用途" min-width="90">
              <template #default="{ row }">{{ purposeOf(row.cl_ord_id) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" class="mt">
      <el-col :span="24">
        <LoserReviewCard :leg="leg" />
        <CommitteeReviewPanel :review="loserDoc?.committee_review" class="mt-half" />
      </el-col>
    </el-row>

    <el-row :gutter="12" class="mt">
      <el-col :span="24">
        <PlanCard />
        <CommitteeReviewPanel :review="planDoc?.next_preview?.committee_review"
                              :guards="planDoc?.next_preview?.guards"
                              placeholder class="mt-half" />
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, provide, ref, watch, onMounted } from 'vue'
import { getLegs, getTrades, type GmLeg, type GmTradeRow } from '../api/gm'
import { getOhlcv } from '../api/home'
import { getLoserReview } from '../api/review'
import { getPlanCard, type PlanCardDoc } from '../api/home'
import { staticToday } from '../api/static'
import PositionsPanel from '../components/cockpit/PositionsPanel.vue'
import LoserReviewCard from '../components/cockpit/LoserReviewCard.vue'
import PlanCard from '../components/plan/PlanCard.vue'
import CommitteeReviewPanel from '../components/review/CommitteeReviewPanel.vue'

const STORAGE_KEY = 'cockpit-leg'
const leg = ref(localStorage.getItem(STORAGE_KEY) || 'main')
provide('cockpit-leg', leg)                     // 注入型组件（PositionsPanel 等）跟随本页选择器
watch(leg, (v) => localStorage.setItem(STORAGE_KEY, v))

const legs = ref<GmLeg[]>([])
const legOptions = computed(() => legs.value.map((l) => ({ label: l.label, value: l.key })))
const day = ref(staticToday())

const trades = ref<GmTradeRow[]>([])
const tradesLoading = ref(false)
const names = ref<Record<string, string>>({})
const loserDoc = ref<Awaited<ReturnType<typeof getLoserReview>>>(null)
const planDoc = ref<PlanCardDoc | null>(null)

const toTs = (sym?: string): string => {
  const [ex, code] = String(sym || '.').split('.')
  const sfx: Record<string, string> = { SHSE: 'SH', SZSE: 'SZ' }
  return `${code}.${sfx[ex] || ex}`
}
const num = (v: unknown, nd = 2): string =>
  typeof v === 'number' && Number.isFinite(v) ? v.toFixed(nd) : '—'
const amountOf = (r: GmTradeRow): string =>
  typeof r.amount === 'number' ? r.amount.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
  : typeof r.price === 'number' && typeof r.volume === 'number'
    ? (r.price * r.volume).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '—'
/** UTC（Z 后缀）→ 本地 HH:MM:SS；非 Z 形态（state 订单史已本地化）直显。 */
const localTime = (v: unknown): string => {
  const s = String(v ?? '')
  if (!s) return '—'
  if (!s.endsWith('Z')) return s.slice(11, 19) || s
  const d = new Date(s)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}
const localDate = (v: unknown): string => {
  const s = String(v ?? '')
  if (!s) return ''
  if (!s.endsWith('Z')) return s.slice(0, 10)
  const d = new Date(s)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
/** 用途（OPEN/CHASE/…）：流水行只带 cl_ord_id——从当日 plan 行/历史行推断不可靠，
 *  静态快照 gm_trades_history 的行自带 purpose 键时直显。 */
const purposeOf = (cid?: string): string => {
  const row = trades.value.find((t) => t.cl_ord_id === cid)
  return String((row as { purpose?: string } | undefined)?.purpose || '—')
}

const dayTrades = computed(() =>
  [...trades.value].filter((t) => localDate(t.created_at) === day.value)
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at))))
const buyCount = computed(() => dayTrades.value.filter((t) => t.side === 1).length)

async function loadLeg() {
  tradesLoading.value = true
  try {
    trades.value = await getTrades({ leg: leg.value })
  } finally {
    tradesLoading.value = false
  }
  // 名称富化：逐标的 ohlcv 快照（缺快照不显示——绝不猜名，TradesTable 同法）
  const uniq = Array.from(new Set(trades.value.map((r) => toTs(r.symbol))))
  const got = await Promise.all(uniq.map(async (s) => {
    const d = await getOhlcv(s).catch(() => null)
    return [s, d?.name ?? ''] as const
  }))
  names.value = Object.fromEntries(got)
}

onMounted(async () => {
  legs.value = await getLegs().catch(() => [] as GmLeg[])
  if (!legs.value.some((l) => l.key === leg.value)) leg.value = 'main'
  loserDoc.value = await getLoserReview().catch(() => null)
  planDoc.value = await getPlanCard().catch(() => null)
  await loadLeg()
})
watch(leg, loadLeg)
</script>

<style scoped>
/* 根容器自管滚动（App 壳 100vh+overflow:hidden，各视图负责自己的滚动区——HomeView 同法） */
.review-page {
  flex: 1;
  overflow-y: auto;
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: var(--qt-space-3, 16px);
}
.review-head { display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.title { display: flex; align-items: baseline; gap: 10px; }
.title h2 { margin: 0; font-size: 18px; }
.sub { color: var(--el-text-color-secondary); font-size: 12.5px; }
.controls { display: flex; gap: 10px; align-items: center; }
.mt { margin-top: 12px; }
.mt-half { margin-top: 8px; }
.sym { font-family: var(--qt-font-mono, monospace); }
.name { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 6px; }
</style>
