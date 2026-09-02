<!--
  OpsHealthView 运维健康面板（全读可视化 P5.4 · 2026-09-03 · /ops）。

  物理意图：系统自己健康吗——三问一屏：①进程拓扑（快照时点：server/掘金
  终端/网关/双腿策略存活灯）②任务台账（近 14 日 job_run：pipeline/晨检/
  播报/预演 × 日期的状态格，失败/跳过格 tooltip 看 message）③告警时间线
  （alerts.log 尾部 200 条，CRITICAL/ERROR 红、WARN 橙、INFO 灰）。
  数据=ops_health.json 快照（时点快照非实时，页头盖 generated_at 戳）。
  「作业驾驶舱」烂尾名分兑现：/jobs 内网语义收拢进本页公开态。
-->
<template>
  <div class="ops-view">
    <el-row :gutter="12">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div class="flex-between">
              <span>进程拓扑 <span class="sub">（快照时点）</span></span>
              <span class="sub">探测 @ {{ doc?.generated_at || '—' }}</span>
            </div>
          </template>
          <div class="lights">
            <div v-for="l in lights" :key="l.label" class="light">
              <span class="dot" :class="l.cls" :title="l.hint">{{ l.ok ? '●' : '○' }}</span>
              <span class="light-label">{{ l.label }}</span>
              <span class="light-val">{{ l.val }}</span>
            </div>
          </div>
          <div class="sub note-line">{{ doc?.processes?.note || '快照时点探测（非实时）' }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" style="margin-top: 12px;">
      <el-col :span="15">
        <el-card shadow="never">
          <template #header>
            <div class="flex-between">
              <span>任务台账 <span class="sub">（近 14 日 {{ jobJobs.length }} 任务 × {{ jobDays.length }} 日）</span></span>
              <span class="sub">{{ runsDone }}/{{ doc?.job_runs.length || 0 }} done</span>
            </div>
          </template>
          <el-table :data="grid" size="small" height="360">
            <el-table-column label="日期" width="96" fixed="left">
              <template #default="{ row }">{{ row.day.slice(5) }}</template>
            </el-table-column>
            <el-table-column v-for="j in jobJobs" :key="j" :label="j" min-width="88">
              <template #default="{ row }">
                <el-tooltip v-if="row.cells[j]" :content="tipOf(row.cells[j])" placement="top">
                  <span class="cell" :class="clsOf(row.cells[j].status)">
                    {{ tagOf(row.cells[j].status) }}
                  </span>
                </el-tooltip>
                <span v-else class="cell none">—</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="9">
        <el-card shadow="never">
          <template #header>
            <div class="flex-between">
              <span>告警时间线 <span class="sub">（{{ doc?.alerts.length || 0 }} 条）</span></span>
            </div>
          </template>
          <el-scrollbar height="360px" v-if="doc?.alerts.length">
            <el-timeline style="padding: 8px 4px 8px 0;">
              <el-timeline-item v-for="(a, i) in doc.alerts" :key="i"
                                :type="tlType(a.level)" :timestamp="a.ts"
                                :hollow="a.level === 'INFO'">
                <span class="lv" :class="`lv-${a.level.toLowerCase()}`">{{ a.level }}</span>
                <span class="alert-msg">{{ a.msg }}</span>
              </el-timeline-item>
            </el-timeline>
          </el-scrollbar>
          <el-empty v-else description="近段无告警记录" :image-size="50" />
        </el-card>
      </el-col>
    </el-row>
    <div class="sub footer-note">{{ doc?.note }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getOpsHealth, type OpsHealthDoc, type JobRunRow } from '../api/ops'

const doc = ref<OpsHealthDoc | null>(null)

/** 台账 pivot：行=业务日（降序），列=任务名（首现序），格=该日该任务最新一次。 */
const grid = computed<Array<{ day: string; cells: Record<string, JobRunRow> }>>(() => {
  const byDay = new Map<string, Record<string, JobRunRow>>()
  for (const r of doc.value?.job_runs ?? []) {          // 已按 started_at 降序
    const cells = byDay.get(r.date) ?? {}
    if (!cells[r.job]) cells[r.job] = r                  // 首见=该日最新一次
    byDay.set(r.date, cells)
  }
  return Array.from(byDay, ([day, cells]) => ({ day, cells }))
})
const jobDays = computed(() => grid.value.map((g) => g.day))
const jobJobs = computed(() => {
  const seen: string[] = []
  for (const g of grid.value) {
    for (const j of Object.keys(g.cells)) if (!seen.includes(j)) seen.push(j)
  }
  return seen
})
const runsDone = computed(() =>
  (doc.value?.job_runs ?? []).filter((r) => r.status === 'done').length)

const tagOf = (s: string) => s === 'done' ? '✓' : s === 'failed' ? '✗' : '⏭'
const clsOf = (s: string) => s === 'done' ? 'ok' : s === 'failed' ? 'bad' : 'skip'
function tipOf(r: JobRunRow): string {
  const dur = (r.started_at && r.finished_at)
    ? `耗时 ${((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000).toFixed(1)}s`
    : ''
  return `${r.status} · ${r.started_at || ''} ${dur}${r.message ? ` · ${r.message}` : ''}`
}

/** 进程存活灯：server/终端 UI/网关/双腿策略 stage（None=探测失败中性灰）。 */
const lights = computed(() => {
  const p = doc.value?.processes ?? {}
  const stageTxt = (s?: string | null) =>
    s == null ? '未知' : s === '3' || s === 'running' ? '运行' : `stage ${s}`
  return [
    { label: '观测 Server', val: p.server?.running ? `pid ${p.server.pids?.join(',')}` : '未见',
      ok: !!p.server?.running, cls: p.server ? (p.server.running ? 'on' : 'off') : 'na',
      hint: 'engine_processes 探测（-m trading / presentation.server.main:app）' },
    { label: '掘金终端 UI', val: p.emgm3 == null ? '未知' : `×${p.emgm3}`,
      ok: (p.emgm3 ?? 0) > 0, cls: p.emgm3 == null ? 'na' : p.emgm3 > 0 ? 'on' : 'off',
      hint: 'emgm3.exe 进程数（多进程正常）' },
    { label: '掘金网关', val: p.gateway == null ? '未知' : `×${p.gateway}`,
      ok: (p.gateway ?? 0) > 0, cls: p.gateway == null ? 'na' : p.gateway > 0 ? 'on' : 'off',
      hint: 'gmterm-serv.exe（7001-7004 本地网关，死=API 面全灭）' },
    ...(p.legs ?? []).map((l) => ({
      label: `策略 · ${l.label}`,
      val: stageTxt(l.stage),
      ok: l.stage === '3' || l.stage === 'running',
      cls: l.stage == null ? 'na' : (l.stage === '3' || l.stage === 'running') ? 'on' : 'off',
      hint: `7002 /v3/strategies stage（数字=终端启动器视角）` })),
  ]
})

const tlType = (lv: string): 'danger' | 'warning' | 'primary' | 'info' =>
  lv === 'CRITICAL' || lv === 'ERROR' ? 'danger'
    : lv === 'WARN' ? 'warning' : lv === 'INFO' ? 'info' : 'primary'

onMounted(async () => {
  doc.value = await getOpsHealth().catch(() => null)
})
</script>

<style scoped>
.ops-view { flex: 1; overflow-y: auto; width: 100%;
            padding: var(--qt-space-3, 16px); background: var(--qt-bg-page); }
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.lights { display: flex; gap: 24px; flex-wrap: wrap; }
.light { display: flex; align-items: center; gap: 6px; }
.dot { font-size: 15px; line-height: 1; }
.dot.on { color: #2eaf62; }
.dot.off { color: #ef5350; }
.dot.na { color: #c0c4cc; }
.light-label { font-size: 13px; color: var(--el-text-color-primary); }
.light-val { font-size: 12px; color: var(--el-text-color-secondary);
             font-family: var(--qt-font-mono, monospace); }
.note-line { margin-top: 8px; }
.cell { display: inline-block; width: 22px; text-align: center; cursor: default;
        border-radius: 4px; font-size: 12px; padding: 1px 0; }
.cell.ok { color: #2eaf62; background: rgba(46, 175, 98, 0.1); }
.cell.bad { color: #fff; background: #ef5350; font-weight: 700; }
.cell.skip { color: var(--qt-warn, #b88230); background: rgba(184, 130, 48, 0.12); }
.cell.none { color: var(--el-text-color-placeholder); }
.lv { font-size: 10px; font-weight: 700; margin-right: 6px; }
.lv-critical, .lv-error { color: #ef5350; }
.lv-warn { color: var(--qt-warn, #b88230); }
.lv-info { color: var(--el-text-color-secondary); }
.alert-msg { font-size: 12px; color: var(--el-text-color-regular); word-break: break-all; }
.footer-note { margin: 10px 4px; }
</style>
