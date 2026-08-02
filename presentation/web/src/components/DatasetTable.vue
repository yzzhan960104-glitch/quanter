<script setup lang="ts">
/**
 * 数据集资产表格（层级一）
 *
 * 消费 DatasetAsset[]，el-table 展示 7 列（数据集/数据源/市场/粒度/区间/最新同步/状态）。
 * 状态列用 el-tag 五色徽章严格镜像后端 status。
 * failed 态用 el-tooltip 悬浮展示 last_error 尾部，无需额外列。
 *
 * 只读化（Phase 1）：撤除原「操作」列与「立即同步」按钮——同步统一由后台 daemon/cron 编排，
 * 前端不再提供手动触发入口。本组件零硬编码数据集名。
 */
import type { DatasetAsset, DatasetStatus } from '@/api/data'

defineProps<{ datasets: DatasetAsset[] }>()

/** 状态 → el-tag type 映射（语义色，沿用 EP 默认 success/warning/danger/info/primary） */
const TAG_TYPE = {
  healthy: 'success', syncing: 'primary', stale: 'warning', missing: 'info', failed: 'danger',
} as const
const tagType = (s: DatasetStatus) => TAG_TYPE[s]

/** 状态 → 中文标签（单一维护点，与后端状态机同源） */
const TAG_LABEL = {
  healthy: '健康', syncing: '同步中', stale: '已过期', missing: '未同步', failed: '失败',
} as const
const tagLabel = (s: DatasetStatus) => TAG_LABEL[s]
</script>

<template>
  <el-table :data="datasets" style="width: 100%" empty-text="暂无数据集（后端未登记）">
    <el-table-column prop="name" label="数据集" min-width="140" />
    <el-table-column prop="source" label="数据源" width="100" />
    <el-table-column prop="market" label="市场" width="80" />
    <el-table-column prop="granularity" label="粒度" width="100" />
    <el-table-column label="数据区间" min-width="190">
      <template #default="{ row }">
        <span v-if="row.data_start">{{ row.data_start }} ~ {{ row.data_end }}</span>
        <span v-else class="muted">—</span>
      </template>
    </el-table-column>
    <el-table-column label="最新同步" width="180">
      <template #default="{ row }">
        <span v-if="row.latest_sync">{{ row.latest_sync }}</span>
        <span v-else class="muted">—</span>
      </template>
    </el-table-column>
    <el-table-column label="状态" width="110">
      <template #default="{ row }">
        <!-- failed 态悬浮展示失败原因尾部（last_error），无需额外列占宽 -->
        <el-tooltip
          v-if="row.status === 'failed' && row.last_error"
          :content="row.last_error"
          placement="top"
          effect="dark"
        >
          <el-tag :type="tagType(row.status)" size="small" effect="dark">
            {{ tagLabel(row.status) }}
          </el-tag>
        </el-tooltip>
        <el-tag v-else :type="tagType(row.status)" size="small" effect="dark">
          {{ tagLabel(row.status) }}
        </el-tag>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.muted { color: var(--qt-text-secondary); }
</style>
