<!--
  AssetCard 资金小部件（一期观测运营层 · Task 12）。

  物理意图：
    驾驶舱「资金资产」卡片。5s 轮询 GET /trading/asset，展示总资产/可用资金两项摘要。
    与 LiveCockpitView 的 asset 块同数据源，但只读、不含下单耦合，服务于综合看板俯瞰。

  Why toFixed(0)：资产以「元」为单位，整数位已足以反映可用资金水位；小数位会拉长视觉、
    挤压综合看板横向空间。若后续要切到「万元」口径再统一改格式化器。

  Why 5s 而非 2s：资产变化频率远低于心跳态（持仓日内才变动），2s 轮询徒增 EMT 网关压力；
    5s 是观测及时性与请求节流的折中。心跳卡（StatusCard）仍 2s，因状态变更需即时感知。

  数值边界守护：
    - 未连接网关时后端返回空字段（cash/total_asset 可能为 0 或缺）→ 显示「—」而非 0，
      避免误以为账户归零（前视/虚假繁荣防线）。
    - 轮询失败保持上次值：单次抖动不让数字闪跳。
-->
<template>
  <el-card shadow="never">
    <template #header><span>资金资产</span></template>
    <div class="asset-grid">
      <div class="asset-item">
        <div class="asset-label">总资产</div>
        <div class="asset-value">{{ asset.total_asset ? asset.total_asset.toFixed(0) : '—' }}</div>
      </div>
      <div class="asset-item">
        <div class="asset-label">可用资金</div>
        <div class="asset-value">{{ asset.cash ? asset.cash.toFixed(0) : '—' }}</div>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
// 路径：本文件在 src/components/cockpit/，api/trading 在 ../../api/trading（2 层）。
import { getAsset, type Asset } from '../../api/trading'

// 资产初值：零值空态，与 LiveCockpitView 保持一致口径。
const asset = ref<Asset>({ cash: 0, total_asset: 0, market_value: 0 })
let timer: ReturnType<typeof setInterval> | null = null

/** 拉资产：失败静默保持上次值，避免轮询抖动导致数字闪跳。 */
async function fetchAsset() {
  try {
    const r = await getAsset()
    asset.value = r.asset
  } catch {
    /* 心跳/断网时 asset 保持上次：观测面板宁可视旧数据也不要显示 0 误导。 */
  }
}

onMounted(() => {
  fetchAsset()
  timer = setInterval(fetchAsset, 5000)
})

onUnmounted(() => {
  if (timer) { clearInterval(timer); timer = null }
})

// 暴露内部状态供测试与父组件调试断言。
defineExpose({ asset, fetchAsset })
</script>

<style scoped>
.asset-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--qt-space-3);
  padding: var(--qt-space-2) 0;
}
.asset-label {
  font-size: var(--qt-fs-caption);
  color: var(--qt-text-secondary);
}
.asset-value {
  font-size: var(--qt-fs-title);
  color: var(--qt-text-primary);
  font-weight: 600;
  margin-top: 4px;
  font-family: var(--qt-font-mono);
}
</style>
