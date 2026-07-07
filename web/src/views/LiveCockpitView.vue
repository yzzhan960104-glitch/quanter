<script setup lang="ts">
/**
 * 实盘中控大屏（路由 /live）
 *
 * 八块：
 *   ① 一键熔断红色大按钮（el-popconfirm 二次确认 → POST /emergency_halt，幂等）
 *   ② 网关心跳灯（2s 轮询 /status，四态严格镜像后端，绝不本地推断）
 *   ③ 连接/断开按钮（Phase 2：disconnected→连接，live→断开）
 *   ④ 资产卡（Phase 2：总资产/可用资金，live 态 5s 轮询 /asset）
 *   ⑤ 下单面板（Phase 2：symbol/qty/side/price/dry_run 开关/confirm → /submit_order）
 *   ⑥ 订单列表（Phase 2：/orders 3s 轮询，每行带撤单按钮）
 *   ⑦ 持仓 Treemap（面积=市值占比，颜色=浮盈红绿）
 *   ⑧ 持仓明细表 + CSV 导出
 *
 * 红线：轮询定时器 onBeforeUnmount 清理（防内存泄漏）；状态完全跟随后端，
 *      断网/锁定立即反映（杜绝"虚假繁荣"）；非 live 态清空 asset/orders/positions。
 *
 * dry_run 双开关（spec §6.1）：前端 dry_run=true（默认）= 模拟（不真下单）；
 *      dry_run=false 走后端 risk_shield 10 关 + env 总闸。
 */
import { ref, shallowRef, computed, onMounted, onBeforeUnmount, markRaw } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { TreemapChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import {
  getStatus, getPositions, emergencyHalt, exportLiveTrades,
  connect, disconnect, submitOrder, cancelOrder, getOrders, getAsset,
  type TradingStatus, type PositionRow, type OrderRow, type Asset,
} from '../api/trading'
import { logger } from '../utils/logger'

use([TreemapChart, TooltipComponent, CanvasRenderer])

const status = ref<TradingStatus>({ connected: false, locked: false, mode: 'unavailable' })
const positions = shallowRef<PositionRow[]>([])
const asset = shallowRef<Asset>({ cash: 0, total_asset: 0, market_value: 0 })
const orders = shallowRef<OrderRow[]>([])
const halting = ref(false)
const halted = ref(false)
const connecting = ref(false)
const submitting = ref(false)

// 下单表单（dry_run 默认 true=模拟；price 默认 null 但表单填 5.0 便于联调）
const orderForm = ref({
  symbol: '510300.SH',
  qty: 100,
  side: 'buy' as 'buy' | 'sell',
  price: 5.0,
  dry_run: true,
  confirm: false,
})

let statusTimer: ReturnType<typeof setInterval> | null = null

// 心跳四态显示映射（圆点颜色 + 中文标签 + 背景色）
const modeDisplay = computed(() => {
  switch (status.value.mode) {
    case 'live': return { color: '#26a69a', label: '已连接', bg: '#0d2818' }
    case 'vetoed_by_risk': return { color: '#ef5350', label: '风控否决', bg: '#2d1014' }
    case 'disconnected': return { color: '#787b86', label: '未连接', bg: '#1e222d' }
    default: return { color: '#d29922', label: '网关未装配', bg: '#2d2410' }
  }
})

/** 下单模式标注（dry_run 开关旁显眼提示，防误触实盘） */
const orderModeLabel = computed(() =>
  orderForm.value.dry_run ? '【模拟】不真下单' : '【实盘】真下单（经 10 关挡板）'
)

async function fetchStatus() {
  try {
    status.value = await getStatus()
    // 仅 live 态拉持仓/资产/订单；其他态清空，避免展示过期数据（虚假繁荣）
    if (status.value.mode === 'live') {
      try { positions.value = (await getPositions()).positions } catch { positions.value = [] }
      try { asset.value = (await getAsset()).asset } catch { /* asset 保持上次 */ }
      try { orders.value = (await getOrders()).orders } catch { orders.value = [] }
    } else {
      positions.value = []
      asset.value = { cash: 0, total_asset: 0, market_value: 0 }
      orders.value = []
    }
  } catch (e) {
    logger.error('心跳轮询失败:', e)
  }
}

onMounted(() => {
  fetchStatus()
  statusTimer = setInterval(fetchStatus, 2000)
})

onBeforeUnmount(() => {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null }
})

// ============ 连接/断开（Phase 2）============
async function onConnect() {
  connecting.value = true
  try {
    await connect()
    ElMessage.success('网关已连接')
    fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail?.msg || e?.response?.data?.detail || e?.message || ''
    ElMessage.error('连接失败：' + detail + '（确认 EMT 凭证 + 仿真账号有效期）')
  } finally {
    connecting.value = false
  }
}

async function onDisconnect() {
  connecting.value = true
  try {
    await disconnect()
    ElMessage.info('网关已断开')
    fetchStatus()
  } catch (e: any) {
    ElMessage.error('断开失败：' + (e?.message || ''))
  } finally {
    connecting.value = false
  }
}

// ============ 下单/撤单（Phase 2）============
async function onSubmitOrder() {
  submitting.value = true
  try {
    const r = await submitOrder({ ...orderForm.value })
    if (r.state === 'DRY_RUN') {
      ElMessage.info('模拟下单已记录（未真下单）：' + r.message)
    } else {
      ElMessage.success(`下单成功 order_id=${r.order_id} (${r.state})`)
    }
    fetchStatus()   // 立即刷新订单列表
  } catch (e: any) {
    const detail = e?.response?.data?.detail?.msg || e?.response?.data?.detail || e?.message || ''
    ElMessage.warning('下单被拒：' + detail)
  } finally {
    submitting.value = false
  }
}

async function onCancelOrder(oid: string) {
  try {
    const r = await cancelOrder(oid)
    ElMessage.info(`撤单已发出 (${r.state})`)
    fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail?.msg || e?.response?.data?.detail || e?.message || ''
    ElMessage.error('撤单失败：' + detail)
  }
}

/** 订单行是否可撤（仅未成交/部成可撤；FILLED/CANCELLED/REJECTED 终态不可撤） */
function isCancelable(state: string): boolean {
  return state === 'SUBMITTED' || state === 'PARTIAL_FILLED'
}

/** 订单行显示用 id（EMT order_emt_id 或 QMT order_id） */
function orderId(row: OrderRow): string {
  return String(row.order_emt_id ?? row.order_id ?? '')
}

/** 方向显示（EMT side: 1=买 2=卖；兼容 QMT 不带 side 的回报） */
function sideLabel(row: OrderRow): string {
  if (row.side === 1) return '买'
  if (row.side === 2) return '卖'
  return '—'
}

async function onHalt() {
  halting.value = true
  try {
    const r = await emergencyHalt()
    halted.value = r.halted
    ElMessage.warning(r.message)
    fetchStatus()   // 立即刷新（应变为 vetoed_by_risk）
  } catch (e: any) {
    ElMessage.error('熔断请求失败：' + (e?.message || ''))
  } finally {
    halting.value = false
  }
}

// ============ CSV 导出 + 运行中策略 ============
const exportRange = ref<[string, string]>(lastNDays(30))
function lastNDays(n: number): [string, string] {
  const end = new Date(); const start = new Date(); start.setDate(start.getDate() - n)
  return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)]
}
const exporting = ref(false)
async function onExport() {
  exporting.value = true
  try {
    await exportLiveTrades(exportRange.value[0], exportRange.value[1])
    ElMessage.success('CSV 已导出（logs/live_trades.csv 区间数据）')
  } catch (e: any) {
    ElMessage.error('导出失败：' + (e?.message || ''))
  } finally {
    exporting.value = false
  }
}

const runningStrategies = computed(() => {
  const set = new Set<string>()
  positions.value.forEach((p) => { if (p.strategy) set.add(p.strategy) })
  return Array.from(set)
})

// ============ Treemap option（面积=市值/数量，颜色=浮盈红绿） ============
const treemapOption = computed(() => {
  const rows = positions.value
  const data = rows.map((r) => ({
    name: r.symbol,
    value: r.market_value ?? r.qty,
    _pnl: r.pnl,
    itemStyle: {
      color: r.pnl === null ? '#3a4049'
        : r.pnl >= 0 ? '#ef5350' : '#26a69a',   // A 股红涨绿跌
    },
  }))
  return markRaw({
    tooltip: {
      formatter: (p: any) => {
        const d = p.data
        const pnl = d._pnl === null || d._pnl === undefined ? '—' : Number(d._pnl).toFixed(0)
        return `${d.name}<br/>数量/市值: ${Number(d.value).toFixed(0)}<br/>浮盈: ${pnl}`
      },
    },
    series: [{
      type: 'treemap',
      data: data.length ? data : [{ name: '无持仓', value: 1, itemStyle: { color: '#2b3139' } }],
      roam: false,
      nodeClick: false,
      breadcrumb: { show: false },
      label: { show: true, formatter: (p: any) => p.name, color: '#fff', fontSize: 11 },
    }],
  })
})
</script>

<template>
  <div class="cockpit-shell">
    <!-- 顶部状态条 + 连接按钮 + 熔断按钮 -->
    <div class="top-bar">
      <div class="heartbeat" :style="{ background: modeDisplay.bg }">
        <span class="dot" :style="{ background: modeDisplay.color }"></span>
        <span class="ht-label" :style="{ color: modeDisplay.color }">{{ modeDisplay.label }}</span>
        <span class="ht-mode">mode={{ status.mode }}</span>
      </div>
      <!-- Phase 2：连接/断开按钮（disconnected→连接，live→断开） -->
      <button
        v-if="status.mode === 'disconnected'"
        class="conn-btn connect" :disabled="connecting"
        @click="onConnect"
      >{{ connecting ? '连接中…' : '连接' }}</button>
      <button
        v-else-if="status.mode === 'live'"
        class="conn-btn disconnect" :disabled="connecting"
        @click="onDisconnect"
      >{{ connecting ? '断开中…' : '断开' }}</button>
      <el-popconfirm
        title="确认触发紧急熔断？网关将锁定，后续发单一律拒绝。"
        confirm-button-text="熔断" cancel-button-text="取消"
        @confirm="onHalt"
      >
        <template #reference>
          <button class="halt-btn" :disabled="halting || halted" :class="{ halted }">
            {{ halted ? '已熔断' : '紧急熔断' }}
          </button>
        </template>
      </el-popconfirm>
    </div>

    <!-- 资产卡 + 运行中策略 + CSV 导出 -->
    <div class="toolbar">
      <div class="stat">
        <span class="stat-k">总资产</span>
        <span class="stat-v">{{ asset.total_asset ? asset.total_asset.toFixed(0) : '—' }}</span>
      </div>
      <div class="stat">
        <span class="stat-k">可用资金</span>
        <span class="stat-v">{{ asset.cash ? asset.cash.toFixed(0) : '—' }}</span>
      </div>
      <div class="stat">
        <span class="stat-k">持仓数</span><span class="stat-v">{{ positions.length }}</span>
      </div>
      <div class="stat">
        <span class="stat-k">运行中策略</span>
        <span class="stat-v">{{ runningStrategies.length ? runningStrategies.join('、') : '—' }}</span>
      </div>
      <div class="export-group">
        <el-date-picker
          v-model="exportRange" type="daterange" value-format="YYYY-MM-DD" size="small"
          start-placeholder="导出起" end-placeholder="导出止" style="width: 240px"
        />
        <el-button size="small" type="primary" plain :loading="exporting" @click="onExport">
          导出 CSV
        </el-button>
      </div>
    </div>

    <!-- Phase 2：下单面板 -->
    <section class="order-panel">
      <div class="chart-title">下单面板（<span :class="orderForm.dry_run ? 'mode-sim' : 'mode-live'">{{ orderModeLabel }}</span>）</div>
      <el-form :model="orderForm" size="small" label-width="64px" class="order-form">
        <el-form-item label="标的">
          <el-input v-model="orderForm.symbol" placeholder="如 510300.SH" style="width: 160px" />
        </el-form-item>
        <el-form-item label="数量">
          <el-input-number v-model="orderForm.qty" :min="100" :step="100" :precision="0" style="width: 140px" />
        </el-form-item>
        <el-form-item label="方向">
          <el-radio-group v-model="orderForm.side">
            <el-radio-button label="buy">买</el-radio-button>
            <el-radio-button label="sell">卖</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="限价">
          <el-input-number v-model="orderForm.price" :min="0.01" :precision="3" :step="0.1" style="width: 140px" />
        </el-form-item>
        <el-form-item label="模式">
          <el-switch v-model="orderForm.dry_run" active-text="模拟" inactive-text="实盘" />
        </el-form-item>
        <el-form-item label="确认">
          <el-checkbox v-model="orderForm.confirm">二次确认下单</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary" :loading="submitting"
            :disabled="status.mode !== 'live'"
            @click="onSubmitOrder"
          >{{ orderForm.dry_run ? '模拟下单' : '实盘下单' }}</el-button>
          <span v-if="status.mode !== 'live'" class="hint">（需先连接网关）</span>
        </el-form-item>
      </el-form>
    </section>

    <!-- Phase 2：订单列表（含撤单按钮） -->
    <section class="orders-card">
      <div class="chart-title">委托订单（实时回报；SUBMITTED/PARTIAL_FILLED 可撤）</div>
      <el-table :data="orders" size="small" empty-text="无订单（或网关未连接）" max-height="180">
        <el-table-column label="订单号" min-width="160">
          <template #default="{ row }">{{ orderId(row) }}</template>
        </el-table-column>
        <el-table-column label="标的" prop="ticker" width="110" />
        <el-table-column label="方向" width="60">
          <template #default="{ row }">{{ sideLabel(row) }}</template>
        </el-table-column>
        <el-table-column label="价格" width="90">
          <template #default="{ row }">{{ row.price ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="已成交" width="80">
          <template #default="{ row }">{{ row.qty_traded ?? 0 }}</template>
        </el-table-column>
        <el-table-column label="剩余" width="80">
          <template #default="{ row }">{{ row.qty_left ?? 0 }}</template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="row.state === 'FILLED' ? 'success' : row.state === 'REJECTED' || row.state === 'FAILED' ? 'danger' : row.state === 'CANCELLED' ? 'info' : 'warning'">
              {{ row.state }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button
              v-if="isCancelable(row.state)" size="small" type="danger" plain
              @click="onCancelOrder(orderId(row))"
            >撤单</el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <!-- 持仓 Treemap -->
    <section class="treemap-card">
      <div class="chart-title">持仓敞口热力图（面积=市值占比，红涨绿跌）</div>
      <v-chart class="treemap" :option="treemapOption" autoresize theme="terminal-dark" />
    </section>

    <!-- 持仓明细表 -->
    <section class="positions-card">
      <div class="chart-title">持仓明细（标的 / 策略 / 建仓因子逻辑 / 浮盈）</div>
      <el-table :data="positions" size="small" empty-text="无持仓（或网关未连接）" max-height="220">
        <el-table-column label="标的" prop="symbol" width="120" />
        <el-table-column label="数量" width="100">
          <template #default="{ row }">{{ row.qty }}</template>
        </el-table-column>
        <el-table-column label="市值" width="110">
          <template #default="{ row }">{{ row.market_value === null ? '—' : row.market_value.toFixed(0) }}</template>
        </el-table-column>
        <el-table-column label="浮盈" width="110">
          <template #default="{ row }">
            <span :style="{ color: row.pnl === null ? '#787b86' : (row.pnl >= 0 ? '#ef5350' : '#26a69a') }">
              {{ row.pnl === null ? '—' : row.pnl.toFixed(0) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="所属策略" width="160">
          <template #default="{ row }">{{ row.strategy || '—' }}</template>
        </el-table-column>
        <el-table-column label="建仓因子逻辑" min-width="220">
          <template #default="{ row }">{{ row.entry_rationale || '—' }}</template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.cockpit-shell {
  padding: 12px; height: 100%; display: flex; flex-direction: column;
  gap: 12px; background: #131722; overflow: auto;
}
.top-bar { display: flex; gap: 12px; align-items: stretch; }
.heartbeat {
  flex: 1; display: flex; align-items: center; gap: 10px; padding: 0 16px;
  border: 1px solid #2b3139; border-radius: 6px; background: #1e222d;
}
.dot { width: 12px; height: 12px; border-radius: 50%; box-shadow: 0 0 8px currentColor; }
.ht-label { font-size: 14px; font-weight: 700; }
.ht-mode { font-size: 11px; color: #787b86; margin-left: auto; font-family: ui-monospace, Menlo, monospace; }

/* Phase 2：连接/断开按钮 */
.conn-btn {
  width: 110px; border: none; border-radius: 6px; cursor: pointer;
  font-size: 14px; font-weight: 700; color: #fff; transition: all 0.15s;
}
.conn-btn.connect { background: linear-gradient(180deg, #26a69a, #00897b); }
.conn-btn.disconnect { background: linear-gradient(180deg, #78909c, #546e7a); }
.conn-btn:hover:not(:disabled) { transform: translateY(-1px); }
.conn-btn:disabled { cursor: not-allowed; opacity: 0.6; }

.halt-btn {
  width: 180px; border: none; border-radius: 6px; cursor: pointer;
  font-size: 15px; font-weight: 700; color: #fff;
  background: linear-gradient(180deg, #ef5350, #c62828);
  box-shadow: 0 0 16px rgba(239, 83, 80, 0.5);
  transition: all 0.15s;
}
.halt-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 0 24px rgba(239, 83, 80, 0.8);
}
.halt-btn:disabled { cursor: not-allowed; opacity: 0.6; }
.halt-btn.halted { animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }

/* 工具条 */
.toolbar {
  display: flex; align-items: center; gap: 20px; padding: 8px 12px;
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px;
  flex-wrap: wrap;
}
.stat { display: flex; align-items: baseline; gap: 6px; }
.stat-k { font-size: 11px; color: #787b86; }
.stat-v { font-size: 13px; color: #d1d4dc; font-weight: 600; font-variant-numeric: tabular-nums; }
.export-group { display: flex; align-items: center; gap: 8px; margin-left: auto; }

/* Phase 2：下单面板 */
.order-panel {
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 10px 14px;
}
.order-form { display: flex; flex-wrap: wrap; gap: 4px 16px; align-items: center; margin-top: 6px; }
.order-form :deep(.el-form-item) { margin-bottom: 0; margin-right: 4px; }
.mode-sim { color: #d29922; font-weight: 700; }
.mode-live { color: #ef5350; font-weight: 700; }
.hint { font-size: 11px; color: #787b86; margin-left: 8px; }

.chart-title { font-size: 13px; color: #d1d4dc; margin-bottom: 6px; }

/* Phase 2：订单列表 */
.orders-card {
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px;
}

.treemap-card {
  flex: 1; min-height: 200px; background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px;
}
.treemap { height: calc(100% - 26px); min-height: 180px; }

.positions-card {
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px;
}
</style>
