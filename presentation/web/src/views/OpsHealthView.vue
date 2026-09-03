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
    <!-- 锚点导航（方案 §〇.6 多卡页）：长页五段一键跳转 -->
    <div class="anchors">
      <a href="#ops-procs">进程</a><a href="#ops-ledger">台账</a>
      <a href="#ops-alerts">告警</a><a href="#ops-backtest">回测×digest</a>
      <span class="sub">快照 @ {{ doc?.generated_at || '—' }}（非实时）</span>
    </div>

    <el-row :gutter="12" id="ops-procs">
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
      <el-col :span="15" id="ops-ledger">
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
      <el-col :span="9" id="ops-alerts">
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
    <el-row :gutter="12" style="margin-top: 12px;" id="ops-backtest">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <div class="flex-between">
              <span>回测队列 <span class="sub">（近 {{ queue?.tasks.length || 0 }} 任务）</span></span>
              <span class="sub">{{ qStat.SUCCESS || 0 }} 成功 / {{ qStat.FAILED || 0 }} 失败</span>
            </div>
          </template>
          <el-table :data="queue?.tasks || []" size="small" height="280"
                    :empty-text="queue ? '暂无回测任务' : '加载中…'">
            <el-table-column label="任务" width="76">
              <template #default="{ row }">
                <span class="mono">{{ row.task }}</span>
              </template>
            </el-table-column>
            <el-table-column label="创建" width="140">
              <template #default="{ row }">{{ String(row.created_at || '').slice(0, 16) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.status === 'SUCCESS' ? 'success'
                  : row.status === 'FAILED' ? 'danger' : 'info'"
                        size="small" effect="plain">
                  {{ row.status === 'SUCCESS' ? '完成' : row.status === 'FAILED' ? '失败' : row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="窗口" min-width="150">
              <template #default="{ row }">{{ row.window }}</template>
            </el-table-column>
            <el-table-column label="参数摘要" min-width="200">
              <template #default="{ row }">
                <span class="mono cfg">{{ cfgOf(row) }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>
            <div class="flex-between">
              <span>回测期望 × 实盘 <span class="sub">（digest {{ queue?.digest?.day || '—' }}）</span></span>
              <el-tag size="small" effect="plain" type="info">{{ queue?.digest?.drift || '—' }}</el-tag>
            </div>
          </template>
          <div v-if="queue?.digest" class="cmp">
            <div class="cmp-row cmp-head">
              <span></span><span>成交笔数</span><span>胜率</span><span>均 rr</span>
            </div>
            <div class="cmp-row">
              <span class="cmp-name">回测期望</span>
              <span class="mono">{{ queue.digest.expect?.trades ?? '—' }}</span>
              <span class="mono">{{ queue.digest.expect?.win_rate ?? '—' }}</span>
              <span class="mono">{{ queue.digest.expect?.avg_rr ?? '—' }}</span>
            </div>
            <div class="cmp-row">
              <span class="cmp-name">实盘（fill 去重）</span>
              <span class="mono">{{ queue.digest.live?.trades ?? '—' }}</span>
              <span class="mono">{{ queue.digest.live?.win_rate ?? '—' }}</span>
              <span class="mono">{{ queue.digest.live?.avg_rr ?? '—' }}</span>
            </div>
            <div class="sub cmp-note">{{ queue.note }}</div>
          </div>
          <el-empty v-else description="digest 摘要累积中" :image-size="50" />
        </el-card>
      </el-col>
    </el-row>
    <div class="sub footer-note">{{ doc?.note }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getOpsHealth, type OpsHealthDoc, type JobRunRow } from '../api/ops'
import { getBacktestQueue, type BacktestQueueDoc, type ReplayTask } from '../api/research'

const doc = ref<OpsHealthDoc | null>(null)
const queue = ref<BacktestQueueDoc | null>(null)

const qStat = computed<Record<string, number>>(() => {
  const s: Record<string, number> = {}
  for (const t of queue.value?.tasks ?? []) s[t.status] = (s[t.status] ?? 0) + 1
  return s
})

/** 参数摘要：cfg 键值对拼一行（长则截）。 */
function cfgOf(t: ReplayTask): string {
  const cfg = t.cfg
  if (!cfg || typeof cfg !== 'object') return '—'
  const s = Object.entries(cfg).map(([k, v]) => `${k}=${String(v)}`).join(' ')
  return s.length > 60 ? s.slice(0, 58) + '…' : s
}

/** 台账 pivot：行=业务日（**降序**），列=任务名（首现序），格=该日该任务最新一次。
 *  code-review HV-2：Map 插入序=started_at 壁钟首现序≠业务日序（brief 类任务
 *  在次一交易日 18:01 补报上一 business_date，每个快照夜都会把 T-1 插到 T 之上）
 *  ——收齐后必须按业务日显式降序。 */
const grid = computed<Array<{ day: string; cells: Record<string, JobRunRow> }>>(() => {
  const byDay = new Map<string, Record<string, JobRunRow>>()
  for (const r of doc.value?.job_runs ?? []) {          // 已按 started_at 降序
    const cells = byDay.get(r.date) ?? {}
    if (!cells[r.job]) cells[r.job] = r                  // 首见=该日最新一次
    byDay.set(r.date, cells)
  }
  return Array.from(byDay, ([day, cells]) => ({ day, cells }))
    .sort((a, b) => (a.day < b.day ? 1 : -1))
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

/** 台账格图标（J-9：running 单列——快照时点 pipeline 未结束即 running，
 *  塌成「跳过」会掩盖在跑事实）。 */
const tagOf = (s: string) => s === 'done' ? '✓' : s === 'failed' ? '✗'
  : s === 'running' ? '◐' : '⏭'
const clsOf = (s: string) => s === 'done' ? 'ok' : s === 'failed' ? 'bad'
  : s === 'running' ? 'run' : 'skip'
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
  queue.value = await getBacktestQueue().catch(() => null)
})
</script>

<style scoped>
.ops-view { flex: 1; overflow-y: auto; width: 100%;
            padding: var(--qt-space-3, 16px); background: var(--qt-bg-page); }
.anchors { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.anchors a { font-size: 12px; color: var(--qt-accent, #2962ff); text-decoration: none;
             padding: 2px 10px; border: 1px solid var(--qt-border, #dcdfe6);
             border-radius: 10px; }
.anchors a:hover { background: rgba(41, 98, 255, 0.08); }
.anchors { scroll-margin-top: 8px; }
#ops-procs, #ops-ledger, #ops-alerts, #ops-backtest { scroll-margin-top: 8px; }
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
.cell.run { color: #2962ff; background: rgba(41, 98, 255, 0.1); font-weight: 700; }
.cell.none { color: var(--el-text-color-placeholder); }
.lv { font-size: 10px; font-weight: 700; margin-right: 6px; }
.lv-critical, .lv-error { color: #ef5350; }
.lv-warn { color: var(--qt-warn, #b88230); }
.lv-info { color: var(--el-text-color-secondary); }
.alert-msg { font-size: 12px; color: var(--el-text-color-regular); word-break: break-all; }
.footer-note { margin: 10px 4px; }
.cmp { padding: 4px 0; }
.cmp-row { display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr; gap: 6px;
           padding: 8px 0; border-bottom: 1px dashed var(--qt-border, #dcdfe6);
           font-size: 13px; align-items: center; }
.cmp-row:last-of-type { border-bottom: none; }
.cmp-head { color: var(--el-text-color-secondary); font-size: 12px; }
.cmp-name { color: var(--el-text-color-primary); font-size: 12px; }
.cmp-note { margin-top: 8px; }
.cfg { font-size: 11px; color: var(--el-text-color-secondary); }
</style>
