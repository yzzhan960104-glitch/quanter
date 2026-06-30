<script setup lang="ts">
/**
 * 沉浸式日志终端：消费 useTerminalState().logs（per-run SSE 事件归一化）。
 *
 * Epic 4 改造核心：不再自带 EventSource 订阅全局 /logs/stream，改为读取
 * useTerminalState 模块级单例的 logs ref。
 *
 * 为何改成消费 composable 而不是组件内自订阅：
 * - 旧版订阅全局 /logs/stream 是后端通用日志通道（含所有模块的日志），
 *   与"本次回测"无强关联，用户在终端看到的日志与正在跑的回测脱节。
 * - Epic 4 后端改为 per-run SSE（/run/stream/{run_id}），只推本次回测的
 *   progress/trade/risk/result 帧；execute() 已把这些帧归一化为 LogEntry
 *   放进单例 logs，TerminalLogs 直接消费即与当前回测强绑定。
 * - 状态归属：日志流的"开/关/清空"由 execute() 统一管理（避免组件挂载/卸载
 *   时乱开乱关流），组件层只负责展示，职责单一更易维护。
 *
 * 保留行为：滚动跟随（用户上翻暂停、回到底部恢复）、级别高亮、2000 上限
 * （上限实际由 composable 控制，组件只读不裁剪）。
 */
import { ref, nextTick, watch } from 'vue'
import { useTerminalState } from '@/composables/useTerminalState'

// 解构拿 logs ref（模块级单例，所有消费者共享同一份数组引用）
const { logs } = useTerminalState()
const follow = ref(true)
const containerRef = ref<HTMLDivElement | null>(null)

// 后端级别映射：ERROR/CRITICAL→红，WARNING→黄，SUCCESS→绿，其余→灰（INFO）
function levelClass(level: string): string {
  switch (level) {
    case 'ERROR':
    case 'CRITICAL':
      return 'lv-error'
    case 'WARNING':
      return 'lv-warn'
    case 'SUCCESS':
      return 'lv-success'
    default:
      return 'lv-info'
  }
}

// ts 为秒级时间戳，转 HH:MM:SS（zh-CN + 24h）显示
function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

async function scrollToBottom() {
  // nextTick 等 DOM 渲染完再滚，否则 scrollHeight 还是旧值
  await nextTick()
  if (follow.value && containerRef.value) {
    containerRef.value.scrollTop = containerRef.value.scrollHeight
  }
}

function onScroll() {
  // 离底 >40px 视为用户主动上翻 → 暂停跟随；回到底部 → 恢复
  const el = containerRef.value
  if (!el) return
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

// 监听 logs 长度变化（push / 清空）触发自动滚动到底
watch(() => logs.value.length, () => scrollToBottom())
</script>

<template>
  <div ref="containerRef" class="term-logs" @scroll="onScroll">
    <div v-for="(l, i) in logs" :key="i" class="log-line">
      <span class="ts">{{ formatTs(l.ts) }}</span>
      <span :class="['lv', levelClass(l.level)]">[{{ l.level }}]</span>
      <span class="msg">{{ l.message }}</span>
    </div>
    <div v-if="!logs.length" class="empty">提交回测后此处实时滚动买卖点与风控告警</div>
  </div>
</template>

<style scoped>
.term-logs {
  width: 100%;
  height: 100%;
  background: #010409; /* 比面板更深的纯黑，强化终端感 */
  color: #c9d1d9;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  padding: 6px 8px;
  overflow-y: auto;
}
.log-line {
  white-space: pre-wrap;
  word-break: break-all;
}
.ts {
  color: #6e7681;
  margin-right: 6px;
}
.lv {
  font-weight: 600;
  margin-right: 6px;
}
.lv-info {
  color: #8b949e;
}
.lv-success {
  color: #3fb950;
}
.lv-warn {
  color: #d29922;
}
.lv-error {
  color: #f85149;
}
.msg {
  color: #c9d1d9;
}
.empty {
  color: #6e7681;
  padding: 8px;
}
</style>
