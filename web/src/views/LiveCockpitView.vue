<script setup lang="ts">
/**
 * 实盘中控大屏（路由 /live）
 *
 * 三块：
 *   ① 一键熔断红色大按钮（el-popconfirm 二次确认 → POST /emergency_halt，幂等）
 *   ② 网关心跳灯（2s 轮询 /status，四态严格镜像后端，绝不本地推断）
 *   ③ 持仓 Treemap（面积=市值占比，颜色=浮盈红绿；第一版不按 sector 分组）
 *
 * 红线：轮询定时器 onBeforeUnmount 清理（防内存泄漏）；状态完全跟随后端，
 *      断网/锁定立即反映（杜绝"虚假繁荣"）。
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
  type TradingStatus, type PositionRow,
} from '../api/trading'
import { logger } from '../utils/logger'

use([TreemapChart, TooltipComponent, CanvasRenderer])

const status = ref<TradingStatus>({ connected: false, locked: false, mode: 'unavailable' })
const positions = shallowRef<PositionRow[]>([])
const halting = ref(false)
const halted = ref(false)

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
    // 仅 live 态拉持仓；断开/锁定/未装配都清空，避免展示过期持仓（虚假繁荣）
    if (status.value.mode === 'live') {
      try {
        positions.value = (await getPositions()).positions
      } catch {
        positions.value = []
      }
    } else {
      positions.value = []
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

// ============ 层级五·CSV 导出 + 运行中策略 ============
// 导出日期区间（默认近 30 天）
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

/** 运行中策略集合（从持仓归因派生：distinct strategy，去 null） */
const runningStrategies = computed(() => {
  const set = new Set<string>()
  positions.value.forEach((p) => { if (p.strategy) set.add(p.strategy) })
  return Array.from(set)
})

// ============ Treemap option（面积=市值/数量，颜色=浮盈红绿） ============
const treemapOption = computed(() => {
  const rows = positions.value
  // 市值缺失（第一版 null）→ 用 qty 作面积代理，颜色中性灰
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
    <!-- 顶部状态条 + 熔断按钮 -->
    <div class="top-bar">
      <div class="heartbeat" :style="{ background: modeDisplay.bg }">
        <span class="dot" :style="{ background: modeDisplay.color }"></span>
        <span class="ht-label" :style="{ color: modeDisplay.color }">{{ modeDisplay.label }}</span>
        <span class="ht-mode">mode={{ status.mode }}</span>
      </div>
      <el-popconfirm
        title="确认触发紧急熔断？网关将锁定，后续发单一律拒绝。"
        confirm-button-text="熔断" cancel-button-text="取消"
        @confirm="onHalt"
      >
        <template #reference>
          <button class="halt-btn" :disabled="halting || halted" :class="{ halted }">
            🚨 {{ halted ? '已熔断' : '紧急熔断' }}
          </button>
        </template>
      </el-popconfirm>
    </div>

    <!-- 层级五·工具条：运行中策略 + CSV 导出 -->
    <div class="toolbar">
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

    <!-- 持仓 Treemap -->
    <section class="treemap-card">
      <div class="chart-title">持仓敞口热力图（面积=市值占比，红涨绿跌）</div>
      <v-chart class="treemap" :option="treemapOption" autoresize theme="terminal-dark" />
    </section>

    <!-- 层级五·持仓明细表（含所属策略 / 建仓因子逻辑） -->
    <section class="positions-card">
      <div class="chart-title">持仓明细（标的 / 策略 / 建仓因子逻辑 / 浮盈）</div>
      <el-table :data="positions" size="small" empty-text="无持仓（或网关未连接）" max-height="240">
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
  gap: 12px; background: #131722;
}
.top-bar { display: flex; gap: 16px; align-items: stretch; }
.heartbeat {
  flex: 1; display: flex; align-items: center; gap: 10px; padding: 0 16px;
  border: 1px solid #2b3139; border-radius: 6px; background: #1e222d;
}
.dot { width: 12px; height: 12px; border-radius: 50%; box-shadow: 0 0 8px currentColor; }
.ht-label { font-size: 14px; font-weight: 700; }
.ht-mode { font-size: 11px; color: #787b86; margin-left: auto; font-family: ui-monospace, Menlo, monospace; }
.halt-btn {
  width: 200px; border: none; border-radius: 6px; cursor: pointer;
  font-size: 16px; font-weight: 700; color: #fff;
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
.treemap-card {
  flex: 1; background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px;
}
.chart-title { font-size: 13px; color: #d1d4dc; margin-bottom: 6px; }
.treemap { height: calc(100% - 26px); }

/* 层级五·工具条与持仓表 */
.toolbar {
  display: flex; align-items: center; gap: 20px; padding: 8px 12px;
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px;
}
.stat { display: flex; align-items: baseline; gap: 6px; }
.stat-k { font-size: 11px; color: #787b86; }
.stat-v { font-size: 13px; color: #d1d4dc; font-weight: 600; font-variant-numeric: tabular-nums; }
.export-group { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.positions-card {
  background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px;
  max-height: 280px; overflow: hidden;
}
</style>
