<!--
  AbHistoryCard 近 N 日对照进度（2026-08-29 多腿方案 §3.2 · ExperimentView）。

  物理意图:校准轮"连续绿"的可视化——近 10 个自然日的每日对照结论(ok/红旗/无数据)
  一行色块,直观回答"还差几天满足晋级闸"。每日一次 /gm/ab 查询(服务端只读 SQL,
  10 次轻查询;非交易日自然呈现"无数据"灰块)。
-->
<template>
  <el-card shadow="never">
    <template #header><span>近 {{ days.length }} 日对照进度</span></template>
    <div class="hist-row">
      <div v-for="d in days" :key="d.date" class="hist-cell" :class="d.state" :title="`${d.date} ${d.title}`">
        <div class="hist-date">{{ d.date.slice(5) }}</div>
        <div class="hist-state">{{ d.label }}</div>
      </div>
    </div>
    <div class="hist-legend">
      <span class="lg ok">✅ 一致</span><span class="lg bad">🔴 红旗</span>
      <span class="lg none">· 无数据</span>(校准轮晋级闸:连续 10 交易日一致)
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getAbDaily } from '../../api/gm'

interface DayCell { date: string; state: 'ok' | 'bad' | 'none'; label: string; title: string }
const days = ref<DayCell[]>([])

function fmtDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

onMounted(async () => {
  const cells: DayCell[] = []
  for (let i = 0; i < 10; i++) {
    const d = new Date()
    d.setDate(d.getDate() - i)
    const date = fmtDate(d)
    try {
      const p = await getAbDaily(date)
      if (p.single_leg) {
        cells.push({ date, state: 'none', label: '—', title: '实验腿未部署' })
      } else if (p.ok) {
        cells.push({ date, state: 'ok', label: '✅', title: '一致' })
      } else {
        cells.push({ date, state: 'bad', label: '🔴', title: (p.flags || []).join(';').slice(0, 80) })
      }
    } catch {
      cells.push({ date, state: 'none', label: '·', title: '查询失败' })
    }
  }
  days.value = cells.reverse()         // 左旧右新
})

defineExpose({ days })
</script>

<style scoped>
.hist-row { display: grid; grid-template-columns: repeat(10, 1fr); gap: var(--qt-space-2); }
.hist-cell { text-align: center; padding: var(--qt-space-2) 0; border-radius: 4px; background: var(--qt-panel-2, #2a2f3a); }
.hist-cell.ok { background: rgba(46, 140, 90, 0.25); }
.hist-cell.bad { background: rgba(180, 60, 60, 0.3); }
.hist-cell.none { opacity: 0.4; }
.hist-date { font-size: 11px; color: var(--qt-text-secondary); }
.hist-state { font-size: 14px; margin-top: 2px; }
.hist-legend { margin-top: var(--qt-space-2); font-size: var(--qt-fs-caption); color: var(--qt-text-secondary); }
.lg { margin-right: var(--qt-space-2); }
</style>
