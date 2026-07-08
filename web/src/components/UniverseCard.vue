<!--
  UniverseCard：固定只读「宏观动能 Top 50 活跃池」卡片

  职责：纯展示，传达"标的由动态池自动选取，不可手动修改"。
  Why 独立组件：ParamForm 去主观化的视觉锚点——把"隐式劫持 symbol"升级为
  显式 UI 元素，研究员一眼明白回测标的是自动选的、无主观代码输入。
-->
<template>
  <div class="universe-card">
    <div class="uc-head">
      <span class="uc-icon">⚡</span>
      <div class="uc-title">宏观动能 Top 50 活跃池</div>
    </div>
    <div class="uc-desc">
      自动从融资增速 top 板块 + 个股动量/换手评分选取，提交时下发
      <code>dynamic_top50</code> 标识，后端路由到 daily_active 湖。
    </div>
    <div class="uc-tag">只读 · 不可手动修改</div>
  </div>
</template>

<script setup lang="ts">
// 纯展示组件，无 props/state
</script>

<style scoped>
.universe-card {
  border: 1px solid var(--qt-border);
  border-left: 3px solid var(--qt-accent);   /* Quant 蓝左条锚定"动态池"语义 */
  border-radius: 6px;
  padding: 10px 12px;
  background: #1a1f2c;
  margin-bottom: 12px;
}
.uc-head { display: flex; align-items: center; gap: 6px; }
.uc-icon { font-size: 14px; }
.uc-title { font-size: 13px; font-weight: 600; color: var(--qt-text-primary); }
.uc-desc {
  font-size: 11px; color: var(--qt-text-secondary); line-height: 1.5; margin-top: 6px;
}
.uc-desc code {
  background: var(--qt-bg-page); padding: 1px 4px; border-radius: 3px;
  color: var(--qt-down); font-family: ui-monospace, Menlo, monospace;
}
.uc-tag {
  display: inline-block; margin-top: 6px; font-size: 10px;
  color: var(--qt-warn); border: 1px solid #d2992244; border-radius: 3px; padding: 1px 6px;
}
</style>
