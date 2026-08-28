<!--
  AuditExplorer 事件流下钻（2026-08-29 多腿方案 §3.2 · ExperimentView）。

  物理意图:terminal_audit 台账(experiments.db,ingest 15:40 落数)的查询面——
  腿/事件过滤 + 倒序时间线。对照红旗的"看原始行"动线终点:AbDailyCard 报差异 →
  此处下钻看该腿该事件的原始 detail。只读;数据更新节奏=ingest(非实时)。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>事件流下钻 · {{ legs.find((l) => l.key === leg)?.label || leg }} · {{ date }}</span>
        <div class="filters">
          <el-select v-model="leg" size="small" style="width: 96px">
            <el-option v-for="l in legs" :key="l.key" :label="l.label" :value="l.key" />
          </el-select>
          <el-input v-model="event" placeholder="事件过滤(如 SIGNAL)" size="small" style="width: 150px" clearable />
          <el-button size="small" :loading="loading" @click="load">查询</el-button>
        </div>
      </div>
    </template>
    <el-table :data="rows" size="small" height="360" v-loading="loading">
      <el-table-column prop="ts" label="时间" width="170" />
      <el-table-column prop="event" label="事件" width="130" />
      <el-table-column label="detail">
        <template #default="{ row }">{{ JSON.stringify(row.detail) }}</template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getAudit, getLegs, type AuditRow, type GmLeg } from '../../api/gm'

const props = defineProps<{ date: string }>()

const legs = ref<GmLeg[]>([])
const leg = ref('main')
const event = ref('')
const rows = ref<AuditRow[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    rows.value = await getAudit({
      leg: leg.value, date: props.date,
      event: event.value.trim() || undefined, limit: 500,
    })
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    legs.value = await getLegs()
  } catch { /* /legs 不可达:腿选择器退化为 main 缺省 */ }
  load()
})

defineExpose({ rows, load })
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.filters { display: flex; gap: var(--qt-space-2); align-items: center; }
</style>
