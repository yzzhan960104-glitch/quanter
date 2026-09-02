<!--
  VersionHistoryCard 版本演进卡（全读可视化 P6.2 · 2026-09-03）。

  物理意图：策略参数怎么走到今天——experiment.store 版本全谱：①演进时间线
  （v 序号/日期/状态 tag/note）②outer 年化条形对比（best_annual 从版本 note
  抽取；R6-8 vs B3 等全谱一眼见——无指标的版本留空不造数）。数据=
  experiment_versions.json 快照（list_versions 只读）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>版本演进 <span class="sub">（{{ rows.length }} 版全谱）</span></span>
        <span class="sub">outer 年化 %（note 抽取，无指标=空）</span>
      </div>
    </template>
    <div class="vlist">
      <div v-for="r in rows" :key="r.experiment_id" class="vrow">
        <span class="vid mono">v{{ r.version ?? '—' }}</span>
        <span class="vdate sub">{{ String(r.created_at || '').slice(2, 10) }}</span>
        <el-tag :type="statusType(r.status)" size="small" effect="plain">
          {{ statusZh(r.status) }}
        </el-tag>
        <div class="vbar-wrap">
          <div v-if="r.best_annual != null" class="vbar"
               :class="(r.best_annual ?? 0) >= 0 ? 'pos' : 'neg'"
               :style="{ width: barW(r.best_annual) }"></div>
          <span v-else class="sub">—</span>
        </div>
        <span class="vval mono" :class="(r.best_annual ?? 0) >= 0 ? 'pos' : 'neg'">
          {{ r.best_annual != null ? `${r.best_annual}%` : '' }}
        </span>
        <span class="vnote" :title="r.note">{{ r.note }}</span>
      </div>
    </div>
    <div class="foot sub">{{ doc?.note }}</div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getVersions, type VersionsDoc, type VersionRow } from '../../api/research'

const doc = ref<VersionsDoc | null>(null)
const rows = computed<VersionRow[]>(() => doc.value?.rows ?? [])

const maxAbs = computed(() =>
  Math.max(1, ...rows.value.map((r) => Math.abs(r.best_annual ?? 0)).filter(Number.isFinite)))
const barW = (v: number | null) => `${Math.min(100, (Math.abs(v ?? 0) / maxAbs.value) * 100)}%`

const statusType = (s: string): 'success' | 'info' | 'warning' | 'primary' =>
  s === 'ACTIVE' ? 'success' : s === 'ARCHIVED' ? 'info'
    : s === 'DISCARDED' ? 'warning' : 'primary'
const statusZh = (s: string) =>
  s === 'ACTIVE' ? '生效' : s === 'ARCHIVED' ? '归档'
    : s === 'DISCARDED' ? '弃用' : s === 'DRAFT' ? '草稿' : s

onMounted(async () => {
  doc.value = await getVersions().catch(() => null)
})
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.mono { font-family: var(--qt-font-mono, monospace); }
.vlist { max-height: 340px; overflow-y: auto; }
.vrow { display: flex; align-items: center; gap: 10px; padding: 4px 0;
        border-bottom: 1px dashed var(--qt-border, #dcdfe6); font-size: 12px; }
.vrow:last-child { border-bottom: none; }
.vid { width: 34px; color: var(--el-text-color-primary); }
.vdate { width: 66px; }
.vbar-wrap { flex: 0 0 180px; height: 10px; background: var(--qt-bg-overlay, #f0f2f5);
             border-radius: 4px; overflow: hidden; display: flex;
             align-items: center; justify-content: flex-end; }
.vbar { height: 100%; border-radius: 4px; }
.vbar.pos { background: linear-gradient(90deg, #2eaf6255, #2eaf62); }
.vbar.neg { background: linear-gradient(90deg, #ef535055, #ef5350); }
.vval { width: 56px; text-align: right; }
.vval.pos { color: #2eaf62; }
.vval.neg { color: #ef5350; }
.vnote { flex: 1; color: var(--el-text-color-secondary); white-space: nowrap;
         overflow: hidden; text-overflow: ellipsis; }
.foot { margin-top: 8px; }
</style>
