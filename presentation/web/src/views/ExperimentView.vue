<!--
  ExperimentView 实验对照视图（2026-08-29 cockpit 多腿方案 §3.1 · P3）。

  物理意图:双腿 A/B 体系的"实验管理面"——轮次状态(RoundCard)+ 当日对照
  (AbDailyCard)+ 连续绿进度(AbHistoryCard)+ 事件流下钻(AuditExplorer)。
  驾驶舱(CockpitView)答"两国度现在怎么样",本视图答"candidate 与 incumbent
  的行为差异是否恰好等于宣称要改的那部分"。

  只读红线:全视图零交易动作;认识论红线:不做收益率对比(仿真读数≠决策证据)。
  date 为视图级状态(默认今天,可回看历史),三个子组件共享。
-->
<template>
  <div class="experiment">
    <el-row :gutter="12">
      <el-col :span="8"><RoundCard /></el-col>
      <el-col :span="16"><AbDailyCard :date="date" /></el-col>
    </el-row>
    <el-row :gutter="12" style="margin-top: 12px;">
      <el-col :span="24"><AbHistoryCard /></el-col>
    </el-row>
    <el-row :gutter="12" style="margin-top: 12px;">
      <el-col :span="24"><AuditExplorer :date="date" /></el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import RoundCard from '../components/experiment/RoundCard.vue'
import AbDailyCard from '../components/experiment/AbDailyCard.vue'
import AbHistoryCard from '../components/experiment/AbHistoryCard.vue'
import AuditExplorer from '../components/experiment/AuditExplorer.vue'

function fmtDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const date = ref(fmtDate(new Date()))
</script>

<style scoped>
.experiment {
  padding: var(--qt-space-3);
  background: var(--qt-bg-page);
  min-height: 100%;
}
</style>
