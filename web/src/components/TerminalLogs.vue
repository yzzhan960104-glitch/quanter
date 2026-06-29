<script setup lang="ts">
/**
 * 沉浸式日志终端：订阅后端 SSE /api/v1/logs/stream，按级别分色高亮，
 * 自动滚动到底（用户上翻则暂停跟随，回到底部自动恢复）。EventSource 自带断线重连。
 */
import { ref, nextTick, onMounted, onBeforeUnmount } from 'vue'

interface LogEntry { ts: number; level: string; logger: string; message: string }

const logs = ref<LogEntry[]>([])
const follow = ref(true)
const containerRef = ref<HTMLDivElement | null>(null)
let es: EventSource | null = null

// 后端 logging 级别：INFO/WARNING/ERROR/CRITICAL（DEBUG 归 info）
function levelClass(level: string): string {
  switch (level) {
    case 'ERROR':
    case 'CRITICAL': return 'lv-error'
    case 'WARNING': return 'lv-warn'
    case 'SUCCESS': return 'lv-success'
    default: return 'lv-info'
  }
}

function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

async function scrollToBottom() {
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

onMounted(() => {
  es = new EventSource('/api/v1/logs/stream')
  es.onmessage = (ev) => {
    try {
      const rec = JSON.parse(ev.data) as LogEntry
      logs.value.push(rec)
      // 防爆内存：保留最近 2000 条
      if (logs.value.length > 2000) logs.value.splice(0, logs.value.length - 2000)
      scrollToBottom()
    } catch {
      /* 忽略坏帧 */
    }
  }
  // es.onerror 由浏览器自动重连，无需手动处理
})

onBeforeUnmount(() => {
  es?.close()
})
</script>

<template>
  <div ref="containerRef" class="term-logs" @scroll="onScroll">
    <div v-for="(l, i) in logs" :key="i" class="log-line">
      <span class="ts">{{ formatTs(l.ts) }}</span>
      <span :class="['lv', levelClass(l.level)]">[{{ l.level }}]</span>
      <span class="msg">{{ l.message }}</span>
    </div>
    <div v-if="!logs.length" class="empty">等待日志流…（提交回测后此处实时滚动）</div>
  </div>
</template>

<style scoped>
.term-logs {
  width: 100%; height: 100%;
  background: #010409;            /* 比面板更深的纯黑，强化终端感 */
  color: #c9d1d9;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.5;
  padding: 6px 8px; overflow-y: auto;
}
.log-line { white-space: pre-wrap; word-break: break-all; }
.ts { color: #6e7681; margin-right: 6px; }
.lv { font-weight: 600; margin-right: 6px; }
.lv-info { color: #8b949e; }
.lv-success { color: #3fb950; }
.lv-warn { color: #d29922; }
.lv-error { color: #f85149; }
.msg { color: #c9d1d9; }
.empty { color: #6e7681; padding: 8px; }
</style>
