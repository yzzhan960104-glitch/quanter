<!--
  LegSelector 腿选择器（2026-08-29 cockpit 多腿方案 §3.1）。

  物理意图：驾驶舱的「当前腿」全局态入口——读 /gm/legs（LEGS 注册表单源），
  segmented 形态在主腿/实验腿间切换，选中值经 localStorage 持久化（刷新不丢），
  并 provide('cockpit-leg') 给 TradesTable 等跟随组件。实验腿未部署时 /legs
  只返回 main——选择器自然退化为单段（无空切换）。
-->
<template>
  <div v-if="legs.length > 1" class="leg-selector">
    <el-segmented v-model="current" :options="options" size="small" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, provide, watch } from 'vue'
import { getLegs, type GmLeg } from '../../api/gm'

const STORAGE_KEY = 'cockpit-leg'
const legs = ref<GmLeg[]>([])
const current = ref<string>(localStorage.getItem(STORAGE_KEY) || 'main')
const options = computed(() =>
  legs.value.map((l) => ({ label: l.label, value: l.key })))

/* 全局腿态：跟随组件（TradesTable 等）inject 此 ref——单一来源，不各自轮询 /legs。 */
provide('cockpit-leg', current)
watch(current, (v) => localStorage.setItem(STORAGE_KEY, v))

onMounted(async () => {
  try {
    legs.value = await getLegs()
    // 持久化的选中腿已下线（如实验腿撤编）→ 回落 main，避免空选中态。
    if (!legs.value.some((l) => l.key === current.value)) {
      current.value = 'main'
    }
  } catch {
    legs.value = []          // /legs 不可达：隐藏选择器（单腿缺省态），不炸视图
  }
})

defineExpose({ legs, current })
</script>

<style scoped>
.leg-selector {
  display: flex;
  justify-content: flex-end;
  padding: 0 var(--qt-space-2) var(--qt-space-2);
}
</style>
