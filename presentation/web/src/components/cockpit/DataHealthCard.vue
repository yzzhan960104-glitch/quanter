<!--
  DataHealthCard 数据健康小部件（一期观测运营层 · Task 12）。

  物理意图：
    驾驶舱「数据湖健康」卡片。轮询 GET /data/datasets，统计 healthy 数量 / 总数，
    一眼看出数据资产整体健康水位。与 DataLakeView 表格同数据源，但只读摘要、无 sync 触发。

  Why 60s 轮询而非 5s：数据集同步是分钟级/小时级任务（schedule 字段），秒级轮询纯浪费；
    60s 足以及时发现同步失败/stale。

  Why 后端只给 healthy 计数而非全量 status：后端 /data/datasets 返回完整 DatasetAsset[]，
    前端 reduce 统计。未来若加 /data/health/summary 端点直接返回 {healthy,total} 再切。

  边界守护：
    - getDatasets 抛错（后端 500 / 网络断）→ 显示「—/—」而非 0/0，避免误判全失联。
    - total=0 时 healthy_rate=0（而非 NaN）：除零守护（CLAUDE.md 数据质量审查）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>数据湖健康</span>
        <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </template>
    <div class="health-row">
      <div class="health-item">
        <div class="health-label">健康/总数</div>
        <div class="health-value">
          {{ errored ? '—/—' : `${healthy}/${total}` }}
        </div>
      </div>
      <div class="health-item">
        <div class="health-label">健康率</div>
        <div class="health-value" :class="{ 'rate-warn': healthyRate < 1 && !errored }">
          {{ errored ? '—' : (healthyRate * 100).toFixed(0) + '%' }}
        </div>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
// 路径：本文件在 src/components/cockpit/，api/data 在 ../../api/data（2 层）。
import { getDatasets } from '../../api/data'

const loading = ref(false)
// errored：最近一次拉取是否失败。失败时显示「—/—」而非 0/0，避免误判全数据湖失联。
const errored = ref(false)
const healthy = ref(0)
const total = ref(0)

// 健康率：除零守护（total=0 时返回 0，不产生 NaN）。
const healthyRate = computed(() => (total.value === 0 ? 0 : healthy.value / total.value))

/** 拉数据集并统计 healthy / total。失败置 errored，让模板切到「—/—」空态。 */
async function load() {
  loading.value = true
  try {
    const list = await getDatasets()
    total.value = list.length
    healthy.value = list.filter((d) => d.status === 'healthy').length
    errored.value = false
  } catch {
    // 拉取失败：保持上次统计 + 标记 errored，UI 显示空态而非闪回 0。
    errored.value = true
  } finally {
    loading.value = false
  }
}

let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  load()
  timer = setInterval(load, 60000)
})

onUnmounted(() => {
  if (timer) { clearInterval(timer); timer = null }
})

defineExpose({ healthy, total, errored, load })
</script>

<style scoped>
.flex-between {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.health-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--qt-space-4);
  padding: var(--qt-space-2) 0;
}
.health-label {
  font-size: var(--qt-fs-caption);
  color: var(--qt-text-secondary);
}
.health-value {
  font-size: var(--qt-fs-title);
  color: var(--qt-text-primary);
  font-weight: 600;
  margin-top: 4px;
  font-family: var(--qt-font-mono);
}
/* 健康率 < 100% 时标黄（--qt-warn），提示有 stale/missing/failed 数据集需关注。 */
.rate-warn {
  color: var(--qt-warn);
}
</style>
