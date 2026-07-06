<script setup lang="ts">
/**
 * 归因面板（层级四）—— 回答「为什么买/卖」
 *
 * 三块：
 *   ① 交易列表（Trade Table）：date/direction/symbol/shares/price/cost；
 *      行悬浮（el-tooltip）展示 signal_rationale / exit_rationale（买卖归因）。
 *   ② 最赚单笔切片：FIFO 配对 buy→sell，取利润最高的一笔，展示买卖价/利润/归因。
 *   ③ 最大回撤区间切片：从 drawdown_series 取最深回撤点 + 前置峰值日。
 *
 * 设计原则（反黑盒）：归因文本全部来自后端 TradeRecord，前端只做配对计算与展示。
 * FIFO 配对是显式单趟遍历（O(n)，无黑盒库），与后端 _compute_cost_basis 加权平均法独立。
 */
import { computed } from 'vue'
import type { TradeRecord, DrawdownPoint } from '@/api/backtest'

const props = defineProps<{
  trades: TradeRecord[]
  drawdown: DrawdownPoint[]
}>()

/** 交易行归因摘要（悬浮展示）：优先 exit_rationale（卖）/ signal_rationale（买） */
function rationaleOf(t: TradeRecord): string {
  if (t.direction === 'sell') return t.exit_rationale || t.reason || '信号驱动减仓'
  if (t.direction === 'buy') return t.signal_rationale || '信号驱动加仓'
  return t.reason || '—'
}

/** 方向 → 中文 + 颜色（A 股红涨绿跌：买红/卖绿） */
function dirMeta(d: string) {
  if (d === 'buy') return { cn: '买入', color: '#ef5350' }
  if (d === 'sell') return { cn: '卖出', color: '#26a69a' }
  return { cn: '失败', color: '#787b86' }
}

/** FIFO 配对：buy 队列按时序消耗到 sell，算每笔 sell 的实现盈亏。 */
interface RoundTrip { buyDate: string; buyPrice: number; sellDate: string; sellPrice: number; shares: number; profit: number; rationale: string }

const roundTrips = computed<RoundTrip[]>(() => {
  const buys: Array<{ date: string; price: number; shares: number }> = []
  const trips: RoundTrip[] = []
  for (const t of props.trades) {
    if (t.direction === 'buy' && t.shares > 0) {
      buys.push({ date: t.date, price: t.price, shares: t.shares })
    } else if (t.direction === 'sell' && t.shares > 0) {
      let remaining = t.shares
      while (remaining > 0 && buys.length) {
        const lot = buys[0]
        const take = Math.min(lot.shares, remaining)
        trips.push({
          buyDate: lot.date, buyPrice: lot.price,
          sellDate: t.date, sellPrice: t.price, shares: take,
          profit: (t.price - lot.price) * take,
          rationale: t.exit_rationale || t.reason || '信号驱动减仓',
        })
        lot.shares -= take
        remaining -= take
        if (lot.shares <= 0) buys.shift()
      }
    }
  }
  return trips
})

/** 最赚单笔（实现盈亏最高的 round-trip；无成交则 null） */
const bestTrade = computed(() => {
  if (!roundTrips.value.length) return null
  return roundTrips.value.reduce((a, b) => (b.profit > a.profit ? b : a))
})

/** 最大回撤区间（drawdown_series 最深点 + 前置峰值日） */
const maxDdSlice = computed(() => {
  if (!props.drawdown.length) return null
  let troughIdx = 0
  props.drawdown.forEach((p, i) => { if (p.drawdown < props.drawdown[troughIdx].drawdown) troughIdx = i })
  const trough = props.drawdown[troughIdx]
  // 前置峰值：trough 之前 nav 最高的日期（drawdown=0 即峰值）
  let peakIdx = 0
  for (let i = 0; i <= troughIdx; i++) {
    if (props.drawdown[i].drawdown > props.drawdown[peakIdx].drawdown) peakIdx = i
  }
  const peak = props.drawdown[peakIdx]
  return { peakDate: peak.date, troughDate: trough.date, drawdown: trough.drawdown }
})

const pct = (v: number) => `${(v * 100).toFixed(2)}%`
const money = (v: number) => v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
</script>

<template>
  <div class="attribution">
    <!-- 切片复盘：最赚单笔 + 最大回撤区间 -->
    <div class="slices">
      <div class="slice-card">
        <div class="slice-title">最赚单笔（FIFO 配对）</div>
        <template v-if="bestTrade">
          <div class="slice-body profit">
            +{{ money(bestTrade.profit) }} 元
          </div>
          <div class="slice-detail">
            {{ bestTrade.buyDate }} @{{ bestTrade.buyPrice.toFixed(2) }}
            → {{ bestTrade.sellDate }} @{{ bestTrade.sellPrice.toFixed(2) }}
            · {{ bestTrade.shares }} 股
          </div>
          <div class="slice-rationale">{{ bestTrade.rationale }}</div>
        </template>
        <div v-else class="slice-empty">无配对成交（仅买入未平仓）</div>
      </div>

      <div class="slice-card">
        <div class="slice-title">最大回撤区间</div>
        <template v-if="maxDdSlice">
          <div class="slice-body loss">{{ pct(maxDdSlice.drawdown) }}</div>
          <div class="slice-detail">
            {{ maxDdSlice.peakDate }} → {{ maxDdSlice.troughDate }}
          </div>
          <div class="slice-rationale">峰值净值到最深回撤点</div>
        </template>
        <div v-else class="slice-empty">无回撤数据</div>
      </div>
    </div>

    <!-- 交易列表（行悬浮展示买卖归因） -->
    <div class="trade-section">
      <div class="section-title">交易列表（{{ trades.length }} 笔，悬浮查看归因）</div>
      <el-table :data="trades" size="small" style="width: 100%" empty-text="无交易" max-height="320">
        <el-table-column label="日期" prop="date" width="110" />
        <el-table-column label="方向" width="70">
          <template #default="{ row }">
            <span :style="{ color: dirMeta(row.direction).color, fontWeight: 600 }">
              {{ dirMeta(row.direction).cn }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="标的" width="110">
          <template #default="{ row }"><span class="mono">{{ row.symbol || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="股数" prop="shares" width="90" />
        <el-table-column label="价格" width="90">
          <template #default="{ row }">{{ row.price.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="成本" width="90">
          <template #default="{ row }">{{ row.cost.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="归因" min-width="220">
          <template #default="{ row }">
            <el-tooltip :content="rationaleOf(row)" placement="top" effect="dark">
              <span class="rationale-cell">{{ rationaleOf(row) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.attribution { display: flex; flex-direction: column; gap: 12px; }
.slices { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.slice-card {
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 10px 12px;
}
.slice-title { font-size: 11px; color: #787b86; margin-bottom: 6px; }
.slice-body { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }
.slice-body.profit { color: #26a69a; }
.slice-body.loss { color: #ef5350; }
.slice-detail { font-size: 11px; color: #b2b5be; margin-top: 4px; font-family: ui-monospace, Menlo, monospace; }
.slice-rationale { font-size: 11px; color: #787b86; margin-top: 2px; }
.slice-empty { font-size: 12px; color: #787b86; padding: 6px 0; }

.trade-section { background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px; }
.section-title { font-size: 12px; color: #d1d4dc; font-weight: 600; margin-bottom: 6px; padding: 0 4px; }
.mono { font-family: ui-monospace, Menlo, monospace; color: #b2b5be; }
.rationale-cell {
  font-size: 11px; color: #b2b5be; cursor: help;
  display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
