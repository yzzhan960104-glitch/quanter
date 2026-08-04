<script setup lang="ts">
/**
 * 作业驾驶舱（路由 /jobs）—— Phase 2 · 前端只读化收官
 *
 * 纯观测视图：当天 pipeline / pre_open 台账 + 启动补跑（catchup）状态。
 * 设计意图是把"调度今天跑没跑、为什么没跑"从一堆 cron 日志里抽出来，
 * 给研究员一个 5 秒看懂当日作业健康度的面板。
 *
 * 【只读边界】无任何操作按钮。台账与 catchup 状态由后端 job_ledger /
 * run_startup_catchup 产出，前端只渲染。补跑/重试赴脚本/CLI/cron，
 * 与 LiveCockpitView 同纪律（前端不下发写指令）。
 *
 * 【轮询纪律】自适应节奏：
 *   - 常态 15s（POLL_NORMAL）：not_started / done / failed 时
 *   - 快态 3s（POLL_FAST）：catchup.state === 'running' 时（实时观察补跑进展）
 *   每次 fetch 末尾按当前 catchup.state 重排定时器（clearInterval + setInterval），
 *   保证 running → done 切换瞬间从 3s 回落到 15s。
 *
 * 【防泄漏】onBeforeUnmount clearInterval，与 LiveCockpitView 同款纪律——
 * 切走路由前必须把定时器清干净，否则 setInterval 会引用已卸载组件的闭包
 * 造成内存泄漏 + 对已销毁 ref 赋值的告警。
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  getJobs,
  type JobsSnapshot,
  type JobRow,
  type CatchupState,
} from '../api/trading'
import { logger } from '../utils/logger'
import { toLocalDateStr } from '../utils/date'

// 进入视图时凝固一次业务日期（防跨午夜漂移：进入时 23:59:59，轮询后跨到次日 00:00:01
// 不应把"昨日台账"误当作"今日"再次查询）。每次重渲染不刷新。
// 用本地时区工具：toISOString 在北京凌晨 0-8 点会取到 UTC 昨日，台账整体错位一天。
const businessDate = toLocalDateStr()

const snapshot = ref<JobsSnapshot | null>(null)
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

const POLL_NORMAL = 15000 // 常态：not_started / done / failed
const POLL_FAST = 3000 // 快态：catchup running（实时追踪补跑进展）

/**
 * 拉取当天台账 + catchup 状态。
 * 失败仅 logger.error，不阻断后续轮询（网络抖动不应让面板彻底停摆）；
 * finally 关 loading（让 v-loading 即使在错误态也能解除）；末尾 reschedule 重排节奏。
 */
async function fetchJobs() {
  loading.value = true
  try {
    snapshot.value = await getJobs(businessDate)
  } catch (e) {
    // 网络错误不崩视图：snapshot 保留上次值（或 null），下一轮询继续尝试
    logger.error('作业驾驶舱 getJobs 失败:', e)
  } finally {
    loading.value = false
  }
  reschedule()
}

/**
 * 按当前 catchup.state 自适应重排定时器：
 * 先 clearInterval 旧 timer（无论快慢态都先清干净），再按状态选间隔新设。
 * 关键不变量：每次重排后至多存在一个活动 timer。
 */
function reschedule() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  const ms = isCatchupRunning.value ? POLL_FAST : POLL_NORMAL
  timer = setInterval(fetchJobs, ms)
}

onMounted(async () => {
  await fetchJobs()
})

onBeforeUnmount(() => {
  // 防泄漏：切走路由前清掉轮询句柄（与 LiveCockpitView 同纪律）
  if (timer) {
    clearInterval(timer)
    timer = null
  }
})

// ============ 状态映射（台账四态 → EP tag type + 中文标签） ============

/** JobRow.status → el-tag type：done 成功/skipped 警告/failed 危险/running 主色 */
function statusType(s: JobRow['status']): 'success' | 'warning' | 'danger' | 'primary' {
  switch (s) {
    case 'done':
      return 'success'
    case 'skipped':
      return 'warning'
    case 'failed':
      return 'danger'
    case 'running':
      return 'primary'
    default:
      return 'primary'
  }
}

/** JobRow.status → 中文标签（用户可读） */
function statusLabel(s: JobRow['status']): string {
  switch (s) {
    case 'running':
      return '执行中'
    case 'done':
      return '已完成'
    case 'skipped':
      return '跳过'
    case 'failed':
      return '失败'
    default:
      return s
  }
}

// ============ catchup 计算属性 ============

/** 当前 catchup.state（snapshot 未到位时按 not_started 兜底） */
const catchupState = computed<CatchupState>(() => snapshot.value?.catchup.state ?? 'not_started')

/** catchup 是否处于 running 态（驱动快轮询 + spinner 显隐） */
const isCatchupRunning = computed(() => catchupState.value === 'running')

/** catchup.state → el-tag type */
const catchupTagType = computed<'primary' | 'success' | 'danger' | 'info'>(() => {
  switch (catchupState.value) {
    case 'running':
      return 'primary'
    case 'done':
      return 'success'
    case 'failed':
      return 'danger'
    default:
      return 'info'
  }
})

/** catchup.state → 中文标签 */
const catchupStateLabel = computed(() => {
  switch (catchupState.value) {
    case 'running':
      return '运行中'
    case 'done':
      return '已完成'
    case 'failed':
      return '失败'
    case 'not_started':
      return '未启动'
    default:
      return catchupState.value
  }
})

/**
 * catchup.result → 文本行数组。后端 run_startup_catchup 返回 dict
 * （{pipeline, brief, pre_open, pre_open_note, error}），把每个 key-value
 * 渲染为一行 `key: value`，研究员一眼看清哪些子任务跑成功 / 哪个出错。
 * result 为 null（未启动/运行中）时返回空数组，模板侧显空态提示。
 */
const catchupResultLines = computed<string[]>(() => {
  const result = snapshot.value?.catchup.result
  if (!result) return []
  return Object.entries(result).map(([k, v]) => `${k}: ${String(v)}`)
})

/** 当前轮询节奏描述（让用户知道面板在以多快的频率刷新） */
const pollHint = computed(() =>
  isCatchupRunning.value ? '快轮询 3s（catchup 运行中）' : '常态轮询 15s',
)
</script>

<template>
  <div class="jobs-view">
    <!-- 页头：标题 + 只读副标题 -->
    <header class="page-header">
      <h1 class="page-title">作业驾驶舱</h1>
      <p class="page-subtitle">
        当天 pipeline / pre_open 台账 + 启动补跑状态（只读观测，无操作）
      </p>
    </header>

    <!-- ① job 状态表：当天每个 job 的状态 + gate 拒因 -->
    <section class="qt-card">
      <div class="card-title">Job 台账 · {{ snapshot?.date ?? businessDate }}</div>
      <el-table
        :data="snapshot?.jobs ?? []"
        size="small"
        v-loading="loading"
        empty-text="今日无 job 记录（或台账未初始化）"
      >
        <el-table-column label="Job" prop="name" min-width="140" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="开始时间" prop="started_at" width="180" />
        <el-table-column label="结束时间" width="180">
          <template #default="{ row }">{{ row.finished_at ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="说明" min-width="280">
          <template #default="{ row }">
            <!-- skipped（gate 未过）黄色提示，failed 红色告警，其余常态 -->
            <span
              class="msg"
              :class="{
                'msg-warn': row.status === 'skipped',
                'msg-fail': row.status === 'failed',
              }"
            >{{ row.message || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>

      <!-- 台账读失败降级提示（后端填 warning 字段时展示） -->
      <div v-if="snapshot?.warning" class="warn-line">
        ⚠ {{ snapshot.warning }}
      </div>
    </section>

    <!-- ② 启动补跑卡：catchup 四态 + running 时 spinner + done/failed 后 result 行 -->
    <section class="qt-card">
      <div class="card-title">
        <!-- running 时显旋转图标，让"正在补跑"在视觉上一目了然 -->
        <span v-if="isCatchupRunning" class="spinner" aria-hidden="true" />
        启动补跑（startup catchup）
        <el-tag size="small" :type="catchupTagType" class="catchup-tag">
          {{ catchupStateLabel }}
        </el-tag>
        <span class="hint">{{ pollHint }}</span>
      </div>

      <!-- catchup.result 行列表（done/failed 后展示）；running/not_started 显空态 -->
      <div v-if="catchupResultLines.length" class="result-list">
        <div v-for="line in catchupResultLines" :key="line" class="result-line mono">
          {{ line }}
        </div>
      </div>
      <div v-else class="hint">
        {{
          isCatchupRunning
            ? '补跑运行中，结果将在完成后展示…'
            : '暂无补跑结果（未启动或尚未产出）'
        }}
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 根容器：纵向 flex + padding + overflow auto（与其他 View 同款骨架） */
.jobs-view {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;
  height: 100%;
  background: var(--qt-bg-page);
  overflow: auto;
}

.page-header {
  padding: 4px 2px;
}
.page-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--qt-text-primary);
  margin: 0 0 4px 0;
}
.page-subtitle {
  font-size: 12px;
  color: var(--qt-text-secondary);
  margin: 0;
  line-height: 1.6;
}

/* 卡片：底色 + 边框（token 风格，与 LiveCockpitView/CockpitView 同源） */
.qt-card {
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: 6px;
  padding: 10px 12px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--qt-text-primary);
  margin-bottom: 8px;
}
.catchup-tag {
  margin-left: 4px;
}

/* 等宽字体（result 行让 true/false/null 等值列对齐，提升可读性） */
.mono {
  font-family: ui-monospace, Menlo, Consolas, monospace;
}

/* 说明列：skipped 黄（gate 未过是预期内的"按设计跳过"），failed 红（真异常） */
.msg {
  color: var(--qt-text-regular);
}
.msg-warn {
  color: #d29922;
}
.msg-fail {
  color: #ef5350;
}

/* 旋转动画：catchup running 时让"正在补跑"在视觉上一目了然 */
.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--qt-text-secondary);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* 次要提示文本：轮询节奏、空态、warning 行 */
.hint {
  font-size: 11px;
  color: var(--qt-text-secondary);
  margin-left: auto;
}
.warn-line {
  margin-top: 8px;
  font-size: 12px;
  color: #d29922;
}

/* catchup result 行列表 */
.result-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 4px;
}
.result-line {
  font-size: 12px;
  color: var(--qt-text-regular);
  line-height: 1.6;
}
</style>
