<!--
  StatusCard 心跳小部件（W4-B 改接掘金，2026-08-28 全库评审清偿）。

  物理意图：
    驾驶舱「终端心跳」卡片。轮询 GET /api/v1/gm/overview（server 只读代理 →
    掘金 7002），按策略 stage 映射三态灯（运行/停止/不可达）。QMT 网关四态
    （unavailable/disconnected/live/vetoed_by_risk）随 P3 退役——本卡语义从
    「QMT 网关心跳」迁移为「掘金终端心跳」，显示口径仍严格镜像后端、绝不本地推断。

  Why 5s 而非 2s：overview 打两次 7002（strategies+account-statuses），2s 轮询对
    终端网关是无谓压力；策略进程态秒级感知无业务必要（看护 5 分钟轮 + 晨检兜底）。
-->
<template>
  <el-card shadow="never">
    <template #header><span>终端心跳</span></template>
    <div class="status-box">
      <span class="dot" :style="{ background: stateDisplay.color }" />
      <div class="status-meta">
        <div class="status-label">{{ stateDisplay.label }}</div>
        <div class="status-mode">{{ subLine }}</div>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { getOverview, type GmOverview } from '../../api/gm'

type HeartState = 'running' | 'stopped' | 'unreachable'
const state = ref<HeartState>('unreachable')
const strategyLine = ref('')
let timer: ReturnType<typeof setInterval> | null = null

/* 三态映射（token 色值，与旧四态映射同风格）：运行绿/停止红/不可达警示黄。 */
const STATE_MAP: Record<HeartState, { color: string; label: string }> = {
  running: { color: 'var(--qt-down)', label: '策略运行中' },
  stopped: { color: 'var(--qt-up)', label: '策略已停止' },
  unreachable: { color: 'var(--qt-warn)', label: '终端不可达' },
}
const stateDisplay = computed(() => STATE_MAP[state.value])
const subLine = computed(() => strategyLine.value || 'mode=gm-terminal')

/** 心跳轮询：失败转「终端不可达」态（不再静默——终端断连本身就是要显示的信息）。 */
async function fetchStatus() {
  try {
    const ov: GmOverview = await getOverview()
    // stage 实测是数字（2026-08-29 双腿实证：终端启动器视角 3=模拟/实盘运行、
    // 1=创建态；agent relaunch 拉起的实验腿进程在跑但 stage 停 1——终端只追踪
    // 自己启动的进程）。三态灯=任一策略运行即绿；字符串形态保留兼容（W4-B 原假设）。
    const running = ov.strategies.some((s) => {
      const st = String(s.stage ?? '').toLowerCase()
      return st === 'running' || Number(s.stage) === 3
    })
    state.value = running ? 'running' : 'stopped'
    strategyLine.value = ov.strategies.length
      ? ov.strategies.map((s) => `${s.name || s.id}:stage${s.stage ?? '?'}`).join(' / ')
      : '无策略'
  } catch {
    state.value = 'unreachable'
    strategyLine.value = ''
  }
}

onMounted(() => {
  fetchStatus()
  timer = setInterval(fetchStatus, 5000)
})

onUnmounted(() => {
  if (timer) { clearInterval(timer); timer = null }
})

defineExpose({ state, fetchStatus })
</script>

<style scoped>
.status-box {
  display: flex;
  align-items: center;
  gap: var(--qt-space-3);
  padding: var(--qt-space-2) 0;
}
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
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 22em;
}
</style>
