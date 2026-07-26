<!--
  ReplayCompare.vue —— 历史回测对比组件（Task 11 · 一期观测运营层）

  物理意图（CLAUDE.md 极简 + 显式原则）：
    把异步回测任务列表里多个 SUCCESS run 的核心统计（胜率 / 最大回撤 / 年化）并列对比，
    让研究员一眼看出哪组参数表现更好，避免在 /lab 单 run 视图间来回切换。

  数据流：
    1. onMounted → listReplayTasks() 拉全部任务 → 只保留 status='SUCCESS'（非 SUCCESS 无 report 可对比）。
    2. 用户在 el-table 勾选 ≤ 5 个任务 → @selection-change 触发 onSelect。
    3. onSelect 串行 getReplayTask(task_id) 取详情 → 拼 compareRows（win_rate/max_drawdown/annualized_return
       格式化为百分比，task_id 截前 8 字符防表格溢出）。

  风控边界（CLAUDE.md 拷问三连）：
    - 数据质量：report 可能 null（非 SUCCESS），onSelect 内 if(d.report) 守护，不渲染缺失行。
    - 数值边界：win_rate/max_drawdown/annualized_return 假定为 0-1 小数，×100 后 toFixed(1)。
      max_drawdown 后端定义为负值（基于累计 rr），格式化后符号保留（如 -10.0%）。
    - 选择上限：selected.slice(0, 5)，防止串行 getReplayTask 拖慢前端（5 个已足够横向对比）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>历史回测对比</span>
        <el-button size="small" @click="loadList">刷新</el-button>
      </div>
    </template>
    <!-- 任务列表：多选 + 关键字段列。height 固定防高度抖动。v-loading 防 loadList 期间空数据闪现。 -->
    <el-table
      :data="tasks"
      size="small"
      height="320"
      @selection-change="onSelect"
      v-loading="loading"
    >
      <el-table-column type="selection" width="40" />
      <el-table-column prop="task_id" label="任务" width="160" />
      <el-table-column prop="created_at" label="时间" width="160" />
      <el-table-column prop="status" label="状态" width="100" />
      <el-table-column prop="progress" label="进度" width="80" />
    </el-table>

    <!-- 对比统计表：仅在有选中任务时显示。 -->
    <div v-if="selected.length" class="compare-stats">
      <el-table :data="compareRows" size="small" border>
        <el-table-column prop="task_id" label="任务" width="120" />
        <el-table-column prop="win_rate" label="胜率" />
        <el-table-column prop="max_drawdown" label="最大回撤" />
        <el-table-column prop="annualized_return" label="年化" />
      </el-table>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { listReplayTasks, getReplayTask, type ReplayTask } from '../../api/caisen'

// 任务列表（loadList 过滤后只含 SUCCESS）。
const tasks = ref<ReplayTask[]>([])
// 当前勾选的任务（最多 5 个，selection-change 由 EP 触发）。
const selected = ref<ReplayTask[]>([])
// 对比统计表数据行（每行 task_id/win_rate/max_drawdown/annualized_return 字符串化）。
const compareRows = ref<{ task_id: string; win_rate: string; max_drawdown: string; annualized_return: string }[]>([])
// 加载态：loadList 期间置 true，控制 el-table v-loading。
const loading = ref(false)

/**
 * loadList —— 拉异步任务列表并过滤 SUCCESS。
 *
 * 只列已完成的（status='SUCCESS'）才有 report 可对比；非 SUCCESS 任务无 report，
 * 即便勾选也是空行，污染对比表故提前在源头过滤掉。
 */
async function loadList() {
  loading.value = true
  try {
    tasks.value = (await listReplayTasks()).filter((t) => t.status === 'SUCCESS')
  } finally {
    loading.value = false
  }
}

/**
 * onSelect —— 多选变更回调，取详情拼对比表。
 *
 * Why slice(0, 5)：EP 多选理论上可勾任意多，但串行 getReplayTask 网络往返线性累加，
 * 上限 5 兼顾横向对比充分性与前端响应速度。
 *
 * Why for-of 串行：并发 Promise.all 在 worker 端会与列表轮询竞争 SQLite 读锁，
 * 串行更稳（5 个详情 × 10ms ≈ 50ms 可接受）。
 */
async function onSelect(sel: ReplayTask[]) {
  selected.value = sel.slice(0, 5)
  compareRows.value = []
  for (const t of selected.value) {
    const d = await getReplayTask(t.task_id)
    // 类型守护：ReplayTaskDetail.report 可选（SUCCESS 时填，非 SUCCESS 为 null）。
    // 即便 tasks 已过滤 SUCCESS，这里仍守一层防御性，防后端返回状态机不一致。
    if (d.report) {
      compareRows.value.push({
        task_id: t.task_id.slice(0, 8),
        win_rate: (d.report.win_rate * 100).toFixed(1) + '%',
        max_drawdown: (d.report.max_drawdown * 100).toFixed(1) + '%',
        annualized_return: (d.report.annualized_return * 100).toFixed(1) + '%',
      })
    }
  }
}

onMounted(loadList)
</script>

<style scoped>
/* 头部 flex 两端对齐（标题左、刷新右），与一期其他卡片风格一致。 */
.flex-between {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* 对比统计表上方留白分隔，避免与任务表挤在一起。 */
.compare-stats {
  margin-top: 16px;
}
</style>
