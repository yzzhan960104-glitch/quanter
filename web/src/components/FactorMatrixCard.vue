<script setup lang="ts">
/**
 * 因子矩阵卡片（层级二·看板单元）
 *
 * 消费 FactorSummary，卡片化展示 label/category/status/grid_computable。
 * 点击触发 click 事件 → FactorManagerView 打开 drill-down drawer。
 *
 * 视觉：左侧状态色竖条（实盘绿/训练黄/退役灰），右上角分类 tag，
 * 底部 grid_computable 徽章（可评估 IC / 仅信号）。
 */
import type { FactorSummary } from '@/api/factors'

defineProps<{ factor: FactorSummary }>()
const emit = defineEmits<{ (e: 'click', f: FactorSummary): void }>()
</script>

<template>
  <div class="factor-card" :class="factor.status" @click="emit('click', factor)">
    <div class="status-bar" />
    <div class="card-body">
      <div class="row1">
        <span class="label">{{ factor.label }}</span>
        <el-tag size="small" effect="plain" round>{{ factor.category }}</el-tag>
      </div>
      <div class="row2">
        <span class="name">{{ factor.name }}</span>
        <span class="badge" :class="{ grid: factor.grid_computable }">
          {{ factor.grid_computable ? '可评估IC' : '仅信号' }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 卡片：暗底 + 左侧状态色竖条；hover 高亮边框，pointer 示意可点 */
.factor-card {
  position: relative;
  display: flex;
  background: #1e222d;
  border: 1px solid #2b3139;
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.1s;
  overflow: hidden;
}
.factor-card:hover { border-color: #2962ff; transform: translateY(-1px); }
.status-bar { width: 3px; flex-shrink: 0; }
.factor-card.live .status-bar { background: #26a69a; }
.factor-card.training .status-bar { background: #d29922; }
.factor-card.deprecated .status-bar { background: #787b86; }

.card-body { padding: 8px 10px; flex: 1; min-width: 0; }
.row1 { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.row1 .label { font-size: 13px; font-weight: 600; color: #d1d4dc; }
.row2 { display: flex; align-items: center; justify-content: space-between; gap: 6px; margin-top: 4px; }
.row2 .name {
  font-size: 11px; color: #787b86; font-family: ui-monospace, Menlo, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.badge {
  font-size: 10px; padding: 1px 6px; border-radius: 3px; flex-shrink: 0;
  background: #2a2e39; color: #787b86;
}
.badge.grid { background: rgba(38, 166, 154, 0.15); color: #26a69a; }
</style>
