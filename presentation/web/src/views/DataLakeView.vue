<script setup lang="ts">
/**
 * 数据湖可视视图（层级一）
 *
 * 业务目标：打破数据黑盒，白盒掌控 9 个 parquet 湖的资产现状与健康度。
 *
 * 交互：
 * - 进入即拉 /datasets；只要存在 syncing 态即每 3s 轮询一次，全部非 syncing 则停轮询省请求。
 * - 离开页面 onBeforeUnmount 清定时器（防内存泄漏，与 ExplorerView 同纪律）。
 *
 * 只读化（Phase 1）：撤除「立即同步」手动触发入口——所有同步统一由后台 daemon/cron 编排，
 * 前端只保留对 syncing 态收敛过程的自适应轮询观测（轮询有意义：后台同步仍会产生 syncing 态）。
 *
 * 反黑盒：数据集清单、状态判定全部来自后端，前端只做反射与轮询编排。
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { getDatasets, type DatasetAsset } from '@/api/data'
import DatasetTable from '@/components/DatasetTable.vue'
import { logger } from '@/utils/logger'

const datasets = ref<DatasetAsset[]>([])
const loading = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null
const POLL_INTERVAL = 3000   // 同步中每 3s 轮询一次状态（平衡实时性与请求量）

/** 是否存在任一 syncing 态（驱动轮询起停） */
const anySyncing = computed(() => datasets.value.some(d => d.status === 'syncing'))

async function fetchDatasets(silent = false) {
  if (!silent) loading.value = true
  try {
    datasets.value = await getDatasets()
  } catch (e: any) {
    logger.error('数据集列表拉取失败:', e)
  } finally {
    loading.value = false
  }
}

/** 按 syncing 态自适应起停轮询（无 syncing 即停，省无效请求） */
function ensurePolling() {
  if (anySyncing.value && !pollTimer) {
    pollTimer = setInterval(() => fetchDatasets(true), POLL_INTERVAL)
  } else if (!anySyncing.value && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(async () => {
  await fetchDatasets()
  ensurePolling()
})
onBeforeUnmount(() => {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
})
</script>

<template>
  <div class="data-lake-view">
    <div class="page-header">
      <div class="title">数据湖资产</div>
      <div class="sub">白盒反射 DATASET_REGISTRY · 状态由 parquet mtime + 哨兵文件联合推导（不引 Beat）</div>
      <el-button size="small" :loading="loading" @click="fetchDatasets()">刷新</el-button>
    </div>
    <div class="table-wrap">
      <DatasetTable :datasets="datasets" />
    </div>
  </div>
</template>

<style scoped>
/* 视图根：撑满 App.vue 路由出口，纵向 flex（页头 + 表格），溢出滚动 */
.data-lake-view {
  flex: 1;
  overflow: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.page-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.page-header .title { font-size: 15px; font-weight: 700; color: var(--qt-text-primary); }
.page-header .sub { font-size: 11px; color: var(--qt-text-secondary); flex: 1; }
.table-wrap {
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: 6px;
  padding: 8px;
}
</style>
