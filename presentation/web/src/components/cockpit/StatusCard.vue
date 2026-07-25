<!--
  StatusCard 心跳小部件（一期观测运营层 · Task 12）。

  物理意图：
    驾驶舱「网关心跳」卡片。2s 轮询 GET /trading/status，四态严格镜像后端
    （unavailable/disconnected/live/vetoed_by_risk），前端只做显示映射、绝不本地推断，
    与 LiveCockpitView 同口径（杜绝「虚假繁荣」——CLAUDE.md 数据质量与鲁棒性审查）。

  Why 单独再抽一个小部件（LiveCockpitView 已有心跳）：
    综合看板是「观测俯瞰」视角，需要把心跳/资金/数据健康三块摘要并排展示，而 LiveCockpitView
    的心跳逻辑绑定下单/撤单等实盘操作，整块搬过来耦合过重；这里抽 ~30 行轻量版只做
    「拉 + 显示」，保持综合看板的扁平与零副作用。

  Why onUnmounted clearInterval：路由切换时若不清，定时器会继续发请求并持有组件作用域，
    长期累积造成内存泄漏（CLAUDE.md 自动化重构与性能榨取·热点扫描）。
-->
<template>
  <el-card shadow="never">
    <template #header><span>网关心跳</span></template>
    <div class="status-box">
      <span class="dot" :style="{ background: modeDisplay.color }" />
      <div class="status-meta">
        <div class="status-label">{{ modeDisplay.label }}</div>
        <div class="status-mode">mode={{ status.mode }}</div>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
// 路径：本文件在 src/components/cockpit/，api/trading 在 ../../api/trading（2 层）。
import { getStatus, type TradingStatus, type GatewayMode } from '../../api/trading'

// 心跳状态：初始 unavailable，与后端 get_status 初值一致。
const status = ref<TradingStatus>({ connected: false, locked: false, mode: 'unavailable' })
let timer: ReturnType<typeof setInterval> | null = null

/**
 * 四态显示映射（颜色 + 中文标签）。
 *
 * 色值用 --qt-* token 而非裸 hex（CLAUDE.md 前端新增样式走 token 勿裸 hex）：
 *   - live → --qt-down（A 股「绿」= 连通/正常，与 LiveCockpitView 心跳灯一致）
 *   - vetoed_by_risk → --qt-up（A 股「红」= 风险/阻断）
 *   - disconnected → --qt-text-secondary（中性灰，非异常只是未连）
 *   - unavailable → --qt-warn（警示黄，网关未装配）
 */
const MODE_MAP: Record<GatewayMode, { color: string; label: string }> = {
  live: { color: 'var(--qt-down)', label: '已连接' },
  vetoed_by_risk: { color: 'var(--qt-up)', label: '风控否决' },
  disconnected: { color: 'var(--qt-text-secondary)', label: '未连接' },
  unavailable: { color: 'var(--qt-warn)', label: '网关未装配' },
}

const modeDisplay = computed(() => MODE_MAP[status.value.mode])

/** 心跳轮询：失败静默吞（下一拍重试），不打扰驾驶舱观测体验。 */
async function fetchStatus() {
  try {
    status.value = await getStatus()
  } catch {
    /* 心跳失败保持上一次状态：避免单次抖动让指示灯闪烁误导观测。 */
  }
}

onMounted(() => {
  fetchStatus()
  timer = setInterval(fetchStatus, 2000)
})

onUnmounted(() => {
  if (timer) { clearInterval(timer); timer = null }
})

// 暴露内部状态供测试与父组件调试断言（vm.status / vm.fetchStatus）。
defineExpose({ status, fetchStatus })
</script>

<style scoped>
.status-box {
  display: flex;
  align-items: center;
  gap: var(--qt-space-3);
  padding: var(--qt-space-2) 0;
}
/* 心跳圆点：固定尺寸 + 圆形，背景随四态映射。 */
.dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-label {
  font-size: var(--qt-fs-title);
  color: var(--qt-text-primary);
}
.status-mode {
  font-size: var(--qt-fs-caption);
  color: var(--qt-text-secondary);
  margin-top: 2px;
}
</style>
