<script setup lang="ts">
/**
 * 参数实验室主视图（/lab）。Spec 2 Task 6 核心。
 *
 * 画布（纵向堆叠）：顶栏（选中标识/状态筛选/＋新建回测）→ 左参数详情(只读)｜右收益走势+统计
 * → 买卖日志 → 任务列表(master)。点任务行 master-detail 灌入上方三区。
 *
 * 轮询（节流省请求）：存在 PENDING/RUNNING 时每 3s 刷 list；选中任务状态/进度变化时重取详情
 *   拿 SUCCESS 的 report；无活跃任务立即停轮询（onUnmounted 兜底清定时器防泄漏）。
 * 边界（spec §11 风控拷问·状态机）：
 *   - 404（任务被删/不存在）→ 清选中 + 提示 + 重拉列表
 *   - 422（参数非法）→ 留抽屉展示后端错误（不关抽屉，便于用户改了再提）
 *   - FAILED → 详情区显 error + 「以此参数重提」按钮
 *   - CANCELLED → 取消信号非同步，轮询到 CANCELLED 终态自然收敛
 *   - 空态 → 任务列表/买卖日志均有空态提示
 *
 * 走势区 vs 买卖日志区职责分离（控制器裁决）：ReplayReportPanel 内置买卖流水表，与下方独立
 *   「买卖日志」区会重复渲染 trades。故走势卡传 :show-trades="false"（仅画曲线+统计+形态+月度），
 *   买卖日志区直接渲染 report.trades（详细流水）。职责分明无重复。
 */
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getConfigSchema, listReplayTasks, getReplayTask,
  submitReplayAsync, cancelReplayTask, deleteReplayTask,
} from '@/api/caisen'
import type { ReplayTask, ReplayTaskDetail, ReplayTaskStatus, ReplayAsyncRequestBody } from '@/api/caisen'
import { PARAM_GROUPS, PARAM_META, isCoreGroup } from '@/components/lab/paramMeta'
import ReplayReportPanel from '@/components/lab/ReplayReportPanel.vue'
import NewReplayDrawer from '@/components/lab/NewReplayDrawer.vue'
import { logger } from '@/utils/logger'

const POLL_MS = 3000
const configSchema = shallowRef<Record<string, any>>({})
const tasks = ref<ReplayTask[]>([])
const selectedId = ref<string | null>(null)
const selected = shallowRef<ReplayTaskDetail | null>(null)
const statusFilter = ref<ReplayTaskStatus | ''>('')
const drawerVisible = ref(false)
const submitting = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

// —— 参数详情（只读）：schema 默认 ∪ 选中任务 cfg_override，按 PARAM_GROUPS 分组 ——
// 物理意图：让用户一眼看到「这次回测生效了哪些参数」——改过默认的高亮（.overridden），
// 未改的显示 schema 默认值。选不同任务时本区随之刷新（master-detail）。
const groupedParamValues = computed(() =>
  PARAM_GROUPS.map((g) => ({
    group: g,
    fields: Object.entries(PARAM_META)
      .filter(([name, m]) => m.group === g && configSchema.value.properties?.[name])
      .map(([name, m]) => {
        const overridden = selected.value?.cfg_override?.[name]
        const def = configSchema.value.properties[name].default
        return { name, title: m.title, value: overridden ?? def, isDefault: overridden === undefined, desc: configSchema.value.properties[name].description }
      }),
  })).filter((g) => g.fields.length),
)

// 参数瘦身 Task 2：详情面板同抽屉分层——形态核心组恒显，高级组（交易执行/时间止损/风控）
// 在「高级」开关后。详情面板为只读展示（非表单），分层纯粹是「收起非形态核心的次要参数」，
// 让用户聚焦「这次回测的形态核心骨架」，需要查执行/风控细节时开 toggle。语义与抽屉一致。
const showAdvanced = ref(false)
const visibleParamGroups = computed(() =>
  groupedParamValues.value.filter((g) => isCoreGroup(g.group) || showAdvanced.value),
)

// 是否存在活跃任务（PENDING/RUNNING）——驱动轮询启停，无活跃即停省请求
const hasActive = computed(() => tasks.value.some((t) => t.status === 'PENDING' || t.status === 'RUNNING'))
const filteredTasks = computed(() =>
  statusFilter.value ? tasks.value.filter((t) => t.status === statusFilter.value) : tasks.value,
)

// 选中任务的展示用辅助：start/end/universe_n 在 ReplayTask 里是可选（可缺省=全市场默认），
// 模板直接插值可能拿到 null/undefined，这里归一为展示串。
const selectedStart = computed(() => selected.value?.start || '—')
const selectedEnd = computed(() => selected.value?.end || '—')
const selectedUniverseLabel = computed(() => {
  const n = selected.value?.universe_n
  return n == null || n === -1 ? '全市场' : (n + ' 只')
})

async function loadSchema() {
  try { configSchema.value = await getConfigSchema() as Record<string, any> }
  catch (e) { logger.error('加载 config schema 失败:', e) }
}
async function loadTasks() {
  try {
    tasks.value = await listReplayTasks()
    // 默认选中最新一条（首次进入或选中被删时）——让用户落地即见结果，免手动点
    if (!selectedId.value && tasks.value.length) await selectTask(tasks.value[0].task_id)
  } catch (e) { logger.error('加载任务列表失败:', e) }
}
async function selectTask(taskId: string) {
  selectedId.value = taskId
  try {
    selected.value = await getReplayTask(taskId)
  } catch (e: any) {
    // 404（被删/不存在）：清选中 + 提示 + 刷新列表（可能被其他端删了，同步本地视图）
    selected.value = null
    selectedId.value = null
    ElMessage.warning('该任务已不存在')
    await loadTasks()
  }
}

async function refreshSelectedIfChanged() {
  if (!selectedId.value) return
  const row = tasks.value.find((t) => t.task_id === selectedId.value)
  if (!row) return
  const cur = selected.value
  // 选中任务状态变化或进度推进 → 重取详情（SUCCESS 时拿完整 report，含 trades/equity_curve）
  if (!cur || row.status !== cur.status || row.progress !== cur.progress) {
    try { selected.value = await getReplayTask(selectedId.value) }
    catch (e) { logger.error('刷新选中详情失败:', e) }
  }
}

async function poll() {
  await loadTasks()
  await refreshSelectedIfChanged()
  // 无活跃任务 → 停轮询（省请求；提交新任务时会重启）
  if (!hasActive.value && pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
function ensurePolling() {
  if (hasActive.value && !pollTimer) pollTimer = setInterval(poll, POLL_MS)
}

// —— 新建回测（抽屉提交） ——
async function onSubmit(body: ReplayAsyncRequestBody) {
  submitting.value = true
  try {
    const { task_id } = await submitReplayAsync(body)
    ElMessage.success('已提交，task_id=' + task_id)
    drawerVisible.value = false
    await loadTasks()
    await selectTask(task_id)   // 选中新任务并起轮询
    ensurePolling()
  } catch (e: any) {
    // 422 参数非法：留抽屉，展示后端错误（不关抽屉，用户可改了再提）
    ElMessage.error('提交失败：' + (e?.response?.data?.detail || e.message || e))
  } finally {
    submitting.value = false
  }
}

async function onCancel(t: ReplayTask) {
  try { await cancelReplayTask(t.task_id); ElMessage.info('取消信号已发送') }
  catch (e: any) { ElMessage.error('取消失败：' + (e?.response?.data?.detail || e)) }
}
async function onDelete(t: ReplayTask) {
  await ElMessageBox.confirm('确认删除该任务记录？', '删除确认', { type: 'warning' })
  try {
    await deleteReplayTask(t.task_id)
    if (selectedId.value === t.task_id) { selectedId.value = null; selected.value = null }
    await loadTasks()
    ElMessage.success('已删除')
  } catch (e: any) { ElMessage.error('删除失败：' + (e?.response?.data?.detail || e)) }
}

onMounted(async () => {
  await Promise.all([loadSchema(), loadTasks()])
  ensurePolling()
})
// 组件卸载必清定时器——否则离开 /lab 后 setInterval 仍在跑，泄漏请求 + 闭包持有已卸载组件 ref
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<template>
  <div class="qt-view-shell lab-view">
    <!-- 顶栏：选中标识 + 状态筛选 + 新建回测入口 -->
    <div class="lab-topbar">
      <span class="lab-title">参数实验室
        <span v-if="selected" class="lab-sub">· {{ selected.status }} · {{ selectedStart }}~{{ selectedEnd }} · {{ selectedUniverseLabel }}</span>
      </span>
      <el-select v-model="statusFilter" placeholder="全部状态" size="small" clearable style="width:120px">
        <el-option label="运行中" value="RUNNING" />
        <el-option label="已完成" value="SUCCESS" />
        <el-option label="失败" value="FAILED" />
        <el-option label="已取消" value="CANCELLED" />
        <el-option label="待运行" value="PENDING" />
      </el-select>
      <el-button type="primary" size="small" @click="drawerVisible = true">＋ 新建回测</el-button>
    </div>

    <!-- 第1层：左参数详情(只读) ｜ 右收益走势+统计 -->
    <div class="lab-row lab-row-top">
      <div class="qt-card lab-params">
        <div class="qt-section-title">
          参数详情
          <!-- 形态核心/高级分层开关（只读面板，仅控可见性，与抽屉分层语义一致） -->
          <el-switch v-model="showAdvanced" size="small" active-text="高级" class="params-adv-switch" />
        </div>
        <div v-for="g in visibleParamGroups" :key="g.group" class="param-group">
          <div class="param-group-name">{{ g.group }}</div>
          <div v-for="f in g.fields" :key="f.name" class="param-line" :title="f.desc">
            <span class="param-line-title">{{ f.title }}</span>
            <span class="param-line-val" :class="{ overridden: !f.isDefault }">{{ f.value }}</span>
          </div>
        </div>
      </div>
      <div class="qt-card lab-chart">
        <div class="qt-section-title">收益率走势</div>
        <!-- SUCCESS：画曲线 + 统计卡（复用 ReplayReportPanel，show-trades=false 省流水表避免与下方重复） -->
        <ReplayReportPanel v-if="selected?.report" :report="selected.report" :show-trades="false" />
        <!-- RUNNING/PENDING：进度占位 -->
        <div v-else-if="selected && (selected.status === 'RUNNING' || selected.status === 'PENDING')"
             class="qt-empty">回测中 {{ selected.progress }}%…</div>
        <!-- FAILED：显 error + 重提入口 -->
        <div v-else-if="selected?.status === 'FAILED'" class="qt-empty lab-failed">
          回测失败：{{ selected.error }}
          <el-button size="small" @click="drawerVisible = true">以此参数重提</el-button>
        </div>
        <div v-else class="qt-empty">选择一个任务查看结果</div>
      </div>
    </div>

    <!-- 第2层：买卖日志（独立区直接渲染 report.trades；与走势卡的 ReplayReportPanel 流水表分离，避免重复） -->
    <div class="qt-card lab-trades">
      <div class="qt-section-title">买卖日志</div>
      <div v-if="selected?.report?.trades?.length" class="lab-trades-list">
        <!-- 流水行：标的/形态/买卖日价/离场原因/盈亏比/持仓天 -->
        <div v-for="(t, i) in selected.report.trades" :key="i" class="trade-row">
          <span>{{ t.symbol }}</span><span>{{ t.pattern_type }}</span>
          <span>{{ t.entry_date }}/{{ t.entry_price }}</span>
          <span>{{ t.exit_date }}/{{ t.exit_price }}</span>
          <span>{{ t.exit_reason }}</span>
          <span :class="t.rr >= 0 ? 'up' : 'down'">{{ t.rr.toFixed(2) }}</span>
          <span>{{ t.holding_bars }}天</span>
        </div>
      </div>
      <div v-else class="qt-empty">暂无买卖日志</div>
    </div>

    <!-- 第3层：任务列表（master；点行灌入上方三区） -->
    <div class="qt-card lab-tasks">
      <div class="qt-section-title">任务列表</div>
      <div v-if="filteredTasks.length" class="lab-task-list">
        <div v-for="t in filteredTasks" :key="t.task_id" class="task-row"
             :class="{ active: t.task_id === selectedId }" @click="selectTask(t.task_id)">
          <span class="task-status" :class="'st-' + t.status">{{ t.status }}</span>
          <span>{{ t.start || '—' }}~{{ t.end || '—' }}</span>
          <span>{{ t.universe_n == null || t.universe_n === -1 ? '全市场' : (t.universe_n + '只') }}</span>
          <span>{{ t.cfg_override?.min_rr_ratio ?? '默认' }}</span>
          <span>{{ t.progress }}%</span>
          <span class="task-ai">[AI Spec3]</span>
          <span class="task-actions">
            <el-button v-if="t.status === 'RUNNING' || t.status === 'PENDING'" size="small" @click.stop="onCancel(t)">取消</el-button>
            <el-button v-if="t.status !== 'RUNNING'" size="small" type="danger" @click.stop="onDelete(t)">删除</el-button>
          </span>
        </div>
      </div>
      <div v-else class="qt-empty">点 ＋新建回测 开始第一次实验</div>
    </div>

    <!-- 新建回测抽屉（v-model:visible 双向；prefill=当前选中任务 cfg 便于微调重跑） -->
    <NewReplayDrawer v-model:visible="drawerVisible" :config-schema="configSchema"
                     :prefill="selected?.cfg_override" :submitting="submitting" @submit="onSubmit" />
  </div>
</template>

<style scoped>
/* 画布：纵向堆叠四区，整体可滚（任务多时不溢出导航） */
.lab-view { display: flex; flex-direction: column; gap: var(--qt-space-2); padding: var(--qt-space-3); height: 100%; overflow: auto; }
.lab-topbar { display: flex; align-items: center; gap: var(--qt-space-2); }
.lab-title { font-weight: 600; color: var(--qt-text-primary); }
.lab-sub { color: var(--qt-text-secondary); font-weight: 400; font-size: 12px; }
.lab-row-top { display: grid; grid-template-columns: 360px 1fr; gap: var(--qt-space-2); }
.lab-params, .lab-chart, .lab-trades, .lab-tasks { padding: var(--qt-space-3); }
/* 参数详情标题行：标题左、高级开关右（分层 Task 2，开关与标题同行省纵向空间） */
.lab-params > .qt-section-title { display: flex; align-items: center; justify-content: space-between; }
.params-adv-switch { margin: 0; }   /* 覆盖 .qt-section-title 的下边距不影响 switch 自身边距 */
.param-group-name { color: var(--qt-accent); font-size: 12px; margin: var(--qt-space-2) 0 2px; }
.param-line { display: flex; justify-content: space-between; font-size: 12px; color: var(--qt-text-regular); padding: 1px 0; }
.param-line-val.overridden { color: var(--qt-up); font-weight: 600; }   /* 改过默认的值高亮（非 A 涨跌语义，仅强调） */
.lab-trades-list, .lab-task-list { display: flex; flex-direction: column; font-size: 12px; }
.trade-row, .task-row { display: grid; grid-template-columns: repeat(7, 1fr); gap: var(--qt-space-2); padding: 3px 0; border-bottom: 1px solid var(--qt-border); }
.task-row { cursor: pointer; grid-template-columns: 70px 1fr 70px 70px 60px 70px 120px; align-items: center; }
.task-row.active { background: rgba(41,98,255,0.12); }   /* 锚定当前选中任务（与导航激活态同色） */
.task-row:hover { background: var(--qt-bg-elevated); }
.up { color: var(--qt-up); } .down { color: var(--qt-down); }
.st-SUCCESS { color: var(--qt-up); } .st-FAILED, .st-CANCELLED { color: var(--qt-down); }
.st-RUNNING { color: var(--qt-accent); } .st-PENDING { color: var(--qt-text-secondary); }
.lab-failed { color: var(--qt-down); }
</style>
