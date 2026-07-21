<!--
  CockpitView 综合看板（一期观测运营层 · Task 12 · 前端收官）。

  物理意图（CLAUDE.md 极简 + 第一性原理）：
    把一期观测运营层的 6 块小部件按「上/中/下」三排聚合到同一屏，提供「俯瞰」视角：
    - 上排：StatusCard 心跳 / AssetCard 资金 / DataHealthCard 数据健康（3 个 ~30 行摘要卡，
      整体反映「网关连不连 / 账户有钱没钱 / 数据新不新」三项运营基本盘）。
    - 中排：TradesTable 流水 / TerminalLogs 日志（左实时成交、右实时日志，并排对照看
      「下单了 → 流水进了 → 日志记了」的链路一致性）。
    - 下排：ReplayCompare 回测对比（横向占满，承载多选 + 对比表，需要更宽横向空间）。

  Why 聚合而非新写：
    Task 9/10/11 已分别落地 TradesTable/TerminalLogs/ReplayCompare，Task 12 又抽了 3 个
    摘要小部件。综合看板只做编排，零业务逻辑，符合「拒绝过度抽象」——这页只是 container。

  Why el-row + el-col 而非 CSS grid：
    项目一期 UI 已全站用 EP（Element Plus），el-row/el-col 的 24 栅格是 EP 标准配套，
    与 LiveCockpitView/DashboardView 同口径；引入 CSS grid 会带来样式体系分裂。

  import 路径（参照 Task 9/11 已验证的相对深度）：
    本文件在 src/views/，组件在 src/components/cockpit/，故 import 用 ../components/cockpit/X
    （只退 1 层到 src/，再进 components/cockpit/）。brief Step 1 骨架即此深度，直接照抄。
-->
<template>
  <div class="cockpit">
    <!-- 上排：三项运营基本盘（心跳 6 / 资金 6 / 数据健康 12，数据健康信息量更大占双倍） -->
    <el-row :gutter="12">
      <el-col :span="6"><StatusCard /></el-col>
      <el-col :span="6"><AssetCard /></el-col>
      <el-col :span="12"><DataHealthCard /></el-col>
    </el-row>

    <!-- 中排：流水 + 日志并排（观测「下单→成交→入日志」链路一致性） -->
    <el-row :gutter="12" style="margin-top: 12px;">
      <el-col :span="12"><TradesTable /></el-col>
      <el-col :span="12"><TerminalLogs /></el-col>
    </el-row>

    <!-- 下排：回测对比横向占满（多选 + 对比表需要更宽空间） -->
    <el-row :gutter="12" style="margin-top: 12px;">
      <el-col :span="24"><ReplayCompare /></el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
// 三个轻量摘要小部件（Task 12 本任务新建）。
import StatusCard from '../components/cockpit/StatusCard.vue'
import AssetCard from '../components/cockpit/AssetCard.vue'
import DataHealthCard from '../components/cockpit/DataHealthCard.vue'
// 一期观测运营层既有组件（Task 9/10/11）。
import TradesTable from '../components/cockpit/TradesTable.vue'
import TerminalLogs from '../components/cockpit/TerminalLogs.vue'
import ReplayCompare from '../components/cockpit/ReplayCompare.vue'
</script>

<style scoped>
/* 综合看板壳：与全站页面底色一致，内边距让 el-row 首块不贴边。 */
.cockpit {
  padding: var(--qt-space-3);
  background: var(--qt-bg-page);
  min-height: 100%;
}
</style>
