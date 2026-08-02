<script setup lang="ts">
/**
 * 实盘中控大屏（路由 /live）—— Phase 1 · 前端只读化
 *
 * 仅保留只读观测能力，前端不再下发任何写指令：
 *   ① 网关心跳灯（2s 轮询 /status，四态严格镜像后端，绝不本地推断）
 *   ② 资产卡（live 态轮询 /asset：总资产/可用资金）
 *   ③ 委托订单回报列表（/orders 轮询，只读，无撤单列）
 *   ④ 持仓 Treemap（面积=市值占比，颜色=浮盈红绿）
 *   ⑤ 持仓明细表
 *   ⑥ CSV 导出（按日期区间，触发浏览器下载）
 *
 * 写操作（连接/断开/下单/撤单/紧急熔断）已撤除——
 *   - 下单：盘前 pre_open cron（09:22）自动挂单，手动补挂走 trading/tools/trigger_pre_open_once.py
 *   - 连接/熔断：赴 QMT 客户端或后端 cron/CLI
 * 后端写接口保留（脚本/CLI/QMT 走），前端只读化仅收回 UI 入口与 API 调用。
 *
 * 红线：轮询定时器 onBeforeUnmount 清理（防内存泄漏）；状态完全跟随后端，
 *      断网/锁定立即反映（杜绝"虚假繁荣"）；非 live 态清空 asset/orders/positions。
 */
import { ref, shallowRef, computed, onMounted, onBeforeUnmount, markRaw } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { TreemapChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import {
  getStatus, getPositions, exportLiveTrades,
  getOrders, getAsset,
  type TradingStatus, type PositionRow, type OrderRow, type Asset,
} from '../api/trading'
import { logger } from '../utils/logger'

use([TreemapChart, TooltipComponent, CanvasRenderer])

const status = ref<TradingStatus>({ connected: false, locked: false, mode: 'unavailable' })
const positions = shallowRef<PositionRow[]>([])
const asset = shallowRef<Asset>({ cash: 0, total_asset: 0, market_value: 0 })
const orders = shallowRef<OrderRow[]>([])

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

/** 订单行显示用 id（QMT seq-str order_id；只读展示，不再用于撤单） */
function orderId(row: OrderRow): string {
  return String(row.order_id ?? '')
}

/** 方向显示（方向码 1=买 2=卖；QMT 回报暂不返回 side，显示 '—'） */
function sideLabel(row: OrderRow): string {
  if (row.side === 1) return '买'
  if (row.side === 2) return '卖'
  return '—'
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
    <!-- 顶部状态条（只读：撤连接/熔断按钮，改显式 READ-ONLY 徽标） -->
    <div class="top-bar">
      <div class="heartbeat" :style="{ background: modeDisplay.bg }">
        <span class="dot" :style="{ background: modeDisplay.color }"></span>
        <span class="ht-label" :style="{ color: modeDisplay.color }">{{ modeDisplay.label }}</span>
        <span class="ht-mode">mode={{ status.mode }}</span>
      </div>
      <!-- 前端只读徽标：下单/连接/熔断请赴 QMT 客户端或 cron -->
      <span class="ro-tag" title="前端只读：下单/连接/熔断请赴 QMT 客户端或 cron">READ-ONLY 只读</span>
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

    <!-- 前端只读提示：撤下单面板，改为指向 cron/CLI 的只读说明 -->
    <section class="readonly-panel">
      <span class="readonly-hint">下单由盘前 pre_open cron（09:22）自动挂单；手动补挂走 trading/tools/trigger_pre_open_once.py；任意单请赴 QMT 客户端。前端只读，不下单。</span>
    </section>

    <!-- 委托订单回报（只读：撤「操作」撤单列，仅展示订单状态流水） -->
    <section class="orders-card">
      <div class="chart-title">委托订单（实时回报，只读）</div>
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
  gap: 12px; background: var(--qt-bg-page); overflow: auto;
}
.top-bar { display: flex; gap: 12px; align-items: stretch; }
.heartbeat {
  flex: 1; display: flex; align-items: center; gap: 10px; padding: 0 16px;
  border: 1px solid var(--qt-border); border-radius: 6px; background: var(--qt-bg-card);
}
.dot { width: 12px; height: 12px; border-radius: 50%; box-shadow: 0 0 8px currentColor; }
.ht-label { font-size: 14px; font-weight: 700; }
.ht-mode { font-size: 11px; color: var(--qt-text-secondary); margin-left: auto; font-family: ui-monospace, Menlo, monospace; }

/* 工具条 */
.toolbar {
  display: flex; align-items: center; gap: 20px; padding: 8px 12px;
  background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px;
  flex-wrap: wrap;
}
.stat { display: flex; align-items: baseline; gap: 6px; }
.stat-k { font-size: 11px; color: var(--qt-text-secondary); }
.stat-v { font-size: 13px; color: var(--qt-text-primary); font-weight: 600; font-variant-numeric: tabular-nums; }
.export-group { display: flex; align-items: center; gap: 8px; margin-left: auto; }

.chart-title { font-size: 13px; color: var(--qt-text-primary); margin-bottom: 6px; }

/* 委托订单回报列表（只读） */
.orders-card {
  background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 8px;
}

.treemap-card {
  flex: 1; min-height: 200px; background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 8px;
}
.treemap { height: calc(100% - 26px); min-height: 180px; }

.positions-card {
  background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 8px;
}

/* Phase 1 · 前端只读化：写操作已撤，新增只读提示样式 */
.ro-tag { font-size: 11px; font-weight: 700; color: #fff; background: #c62828; padding: 2px 8px; border-radius: 4px; margin-left: 8px; }
.readonly-panel { background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 10px 14px; }
.readonly-hint { font-size: 12px; color: var(--qt-text-secondary); line-height: 1.7; }
</style>
