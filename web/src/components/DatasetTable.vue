<script setup lang="ts">
/**
 * 数据集资产表格（层级一）
 *
 * 消费 DatasetAsset[]，el-table 展示 8 列 + 操作列。
 * 状态列用 el-tag 五色徽章严格镜像后端 status；操作列「立即同步」按钮在
 * syncing 时禁用 + loading（防重复触发，与后端哨兵幂等保护双保险）。
 * failed 态用 el-tooltip 悬浮展示 last_error 尾部，无需额外列。
 *
 * 反黑盒：表格数据/列名全部来自后端 DatasetAsset，本组件零硬编码数据集名。
 */
import type { DatasetAsset, DatasetStatus } from '@/api/data'

defineProps<{ datasets: DatasetAsset[] }>()
const emit = defineEmits<{ (e: 'sync', key: string): void }>()

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
    <el-table-column label="操作" width="120" fixed="right">
      <template #default="{ row }">
        <el-button
          size="small"
          type="primary"
          plain
          :loading="row.status === 'syncing'"
          :disabled="row.status === 'syncing'"
          @click="emit('sync', row.key)"
        >
          立即同步
        </el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.muted { color: #787b86; }
</style>
