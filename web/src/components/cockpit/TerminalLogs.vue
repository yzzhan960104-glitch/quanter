<!--
  TerminalLogs 实时日志组件（Task 10 · 一期观测运营层）。

  物理意图：
    订阅后端 SSE 端点 GET /api/v1/logs/stream，把每条 data 行追加到环形缓冲，
    按 ERROR/WARN 级别着色，支持暂停继续，onUnmounted 关闭连接防止内存泄漏。

  关键设计：
    - 环缓冲 MAX=500：服务端可能短时间内推大量日志（如回放/批量任务），
      无上限会导致 DOM 节点数与响应式开销线性膨胀，页面卡死。到达上限后
      shift 最旧一条，保持窗口稳定。
    - requestAnimationFrame 滚底：直接在 push 后赋值 scrollTop 可能因为
      Vue 还没把新 <pre> patch 到 DOM 上而滚不到真正底部；rAF 等到下一帧
      渲染完成再滚动，保证滚到最新行。
    - 暂停状态下仍消费事件但不入列：比 close+重连更稳，避免暂停期间丢日志
      的同时保持连接复用；继续时无需重连开销。
    - onUnmounted close()：组件销毁必须显式关闭 SSE，否则浏览器会一直持有
      连接与回调引用，组件实例无法 GC（路由切换频繁时是真实内存泄漏点）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>实时日志</span>
        <el-button size="small" @click="paused = !paused">{{ paused ? '继续' : '暂停' }}</el-button>
      </div>
    </template>
    <div class="terminal" ref="box">
      <pre v-for="(l, i) in lines" :key="i" :class="levelClass(l)">{{ l }}</pre>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

// 环缓冲上限：防内存膨胀。日志爆发期（回放/批量任务）超过此值则丢弃最旧行。
const MAX = 500

const lines = ref<string[]>([])
const paused = ref(false)
const box = ref<HTMLElement | null>(null)
// EventSource 句柄：setup 作用域持有，onUnmounted 时关闭。
// 用 let 而非 ref：无需响应式追踪，避免被 Vue 代理后丢失原型。
let _es: EventSource | null = null

/**
 * 按日志级别返回着色类。
 * 仅做子串匹配而非正则：后端日志格式为 `YYYY-MM-DD HH:MM:SS LEVEL ...`，
 * LEVEL 字段固定大写，子串命中即足够；正则在此处是过度设计。
 */
function levelClass(l: string) {
  if (l.includes('ERROR')) return 'lvl-error'
  if (l.includes('WARN')) return 'lvl-warn'
  return ''
}

onMounted(() => {
  // 订阅 SSE：服务端 text/event-stream，每条 `data: <line>\n\n` 触发一次 message。
  _es = new EventSource('/api/v1/logs/stream')
  _es.addEventListener('message', (e: MessageEvent) => {
    // 暂停时丢弃：不 close 连接以保留恢复能力，但本次推流不入列。
    if (paused.value) return
    lines.value.push(e.data)
    // 环缓冲截断：shift 是 O(n)，但 MAX=500 下可忽略；若后续上调到 10k+ 再换 deque。
    if (lines.value.length > MAX) lines.value.shift()
    // 自动滚到底：rAF 等 Vue patch 完新 <pre> 再读 scrollHeight，确保滚到真正底部。
    requestAnimationFrame(() => {
      if (box.value) box.value.scrollTop = box.value.scrollHeight
    })
  })
})

onUnmounted(() => {
  // 必须显式关闭：否则浏览器保持 TCP 连接 + 回调闭包持有组件作用域，路由切换后泄漏。
  _es?.close()
  _es = null
})

// 暴露给测试与父组件调试：测试通过 vm.lines / vm._es 断言内部状态。
defineExpose({ _es, lines, paused })
</script>

<style scoped>
.terminal {
  height: 320px;
  overflow-y: auto;
  background: #0d1117;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
}
.terminal pre {
  margin: 0;
  color: #c9d1d9;
  white-space: pre-wrap;
  word-break: break-all;
}
.lvl-error {
  color: #f85149;
}
.lvl-warn {
  /* 走业务 token：tokens.css 已定义 --qt-warn: #d29922（同值），零视觉变化。
     抽 token 后，若后续警示色统一调整改一处即全站生效（CLAUDE.md 前端走 token 勿裸 hex）。 */
  color: var(--qt-warn);
}
</style>
