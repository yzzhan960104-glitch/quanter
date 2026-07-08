<script setup lang="ts">
/**
 * 数据湖可视视图（层级一）
 *
 * 业务目标：打破数据黑盒，白盒掌控 9 个 parquet 湖的资产现状与健康度。
 *
 * 交互：
 * - 进入即拉 /datasets；只要存在 syncing 态即每 3s 轮询一次，全部非 syncing 则停轮询省请求。
 * - 「立即同步」→ POST /sync/{key} → 乐观本地置 syncing → 等下一轮轮询接管真实状态。
 * - 离开页面 onBeforeUnmount 清定时器（防内存泄漏，与 ExplorerView 同纪律）。
 *
 * 反黑盒：数据集清单、状态判定全部来自后端，前端只做反射与轮询编排。
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { getDatasets, triggerSync, type DatasetAsset } from '@/api/data'
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

async function onSync(key: string) {
  // 乐观更新：本地立即置 syncing，避免等下个轮询周期才看到状态翻转
  const row = datasets.value.find(d => d.key === key)
  if (row) row.status = 'syncing'
  try {
    const r = await triggerSync(key)
    ElMessage.success(r.message)
    ensurePolling()
  } catch (e: any) {
    // 失败回滚（下一轮 fetch 会校正为真实态）
    ElMessage.error('触发同步失败：' + (e?.message || ''))
    fetchDatasets(true)
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
      <DatasetTable :datasets="datasets" @sync="onSync" />
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
