<script setup lang="ts">
/**
 * 末态持仓快照表
 *
 * 渲染 SingleBacktestResponse.positions（回测结束时刻的持仓行）。
 * 空数组时 el-table 自动显示 empty-text="暂无持仓"（组合回测持仓未接入前的兜底）。
 *
 * 设计意图（反黑盒）：市值列直接用 toLocaleString 千分位格式化，
 * 不引入额外的格式化库；保留 0 位小数避免长数字撑爆窄列。
 */
import type { PositionRow } from '@/api/backtest'

defineProps<{ positions: PositionRow[] }>()
</script>

<template>
  <div class="pos-card">
    <div class="title">持仓快照</div>
    <el-table :data="positions" size="small" empty-text="暂无持仓" :border="false">
      <el-table-column prop="symbol" label="标的" min-width="90" />
      <el-table-column prop="qty" label="数量" width="70" align="right" />
      <el-table-column label="市值" width="90" align="right">
        <template #default="{ row }">
          {{ row.market_value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) }}
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.pos-card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 6px; }
.title { color: #8b949e; font-size: 12px; margin-bottom: 4px; }
</style>
