<!--
  RoundCard 轮次状态卡（2026-08-29 多腿方案 §3.2 · ExperimentView）。

  物理意图：当前 A/B 轮次的元信息一屏——轮次类型(校准/候选)、candidate 描述、
  起始日、晋级闸。数据源=/gm/round(透传 emquant/config/ab_round.json 手工档案);
  档案缺失显示占位(展示层缺数据≠事故,轮次档案本来就是人工立案物)。

  认识论红线:本卡只显示轮次元信息,永不显示"实验腿收益 vs 主腿"类对比。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>当前轮次</span>
        <el-tag v-if="round" :type="round.type === 'candidate' ? 'warning' : 'info'" size="small">
          {{ round.type === 'candidate' ? '候选轮' : '校准轮' }}
        </el-tag>
      </div>
    </template>
    <template v-if="round">
      <div class="round-line"><span class="k">轮次</span><span>{{ round.round_id }}</span></div>
      <div class="round-line"><span class="k">candidate</span><span>{{ round.candidate }}</span></div>
      <div class="round-line"><span class="k">起始</span><span>{{ round.start }}</span></div>
      <div class="round-line"><span class="k">预期 diff</span><span>{{ round.expect_diff || '—' }}</span></div>
      <div class="round-line"><span class="k">晋级闸</span><span>{{ round.promote_gate || '—' }}</span></div>
    </template>
    <div v-else class="round-empty">轮次档案缺失（ab_round.json）——按 emquant/config 模板立案</div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getRound, type GmRound } from '../../api/gm'

const round = ref<GmRound | null>(null)

onMounted(async () => {
  round.value = await getRound()
})

defineExpose({ round })
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.round-line { display: flex; gap: var(--qt-space-3); padding: 2px 0; font-size: var(--qt-fs-body); }
.round-line .k { width: 72px; flex: none; color: var(--qt-text-secondary); font-size: var(--qt-fs-caption); }
.round-empty { color: var(--qt-text-secondary); font-size: var(--qt-fs-caption); }
</style>
