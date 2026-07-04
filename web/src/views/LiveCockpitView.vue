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
  getStatus, getPositions, emergencyHalt,
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

    <!-- 持仓 Treemap -->
    <section class="treemap-card">
      <div class="chart-title">持仓敞口热力图（面积=市值占比，红涨绿跌）</div>
      <v-chart class="treemap" :option="treemapOption" autoresize theme="terminal-dark" />
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
</style>
