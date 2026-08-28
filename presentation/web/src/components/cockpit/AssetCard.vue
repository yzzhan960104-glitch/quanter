<!--
  AssetCard 资金小部件（一期观测运营层 · Task 12）。

  物理意图：
    驾驶舱「资金资产」卡片（W4-B 改接掘金，2026-08-28 评审清偿）。5s 轮询
    GET /api/v1/gm/asset（server 只读代理 → 掘金 7002 cash 端点），展示
    总权益(nav)/可用资金(available)两项摘要。只读、不含交易耦合。

  Why toFixed(0)：资产以「元」为单位，整数位已足以反映可用资金水位；小数位会拉长视觉、
    挤压综合看板横向空间。若后续要切到「万元」口径再统一改格式化器。

  Why 5s 而非 2s：资产变化频率远低于心跳态（持仓日内才变动），2s 轮询徒增 QMT 网关压力；
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
        <div class="asset-label">总权益</div>
        <div class="asset-value">{{ fmt(asset.nav) }}</div>
      </div>
      <div class="asset-item">
        <div class="asset-label">可用资金</div>
        <div class="asset-value">{{ fmt(asset.available) }}</div>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
// 路径：本文件在 src/components/cockpit/，api/gm 在 ../../api/gm（2 层）。
import { getAsset, type GmAsset } from '../../api/gm'

// 资产初值：空态（掘金口径 nav/available，字段缺失显示「—」防误读归零）。
const asset = ref<GmAsset>({})
let timer: ReturnType<typeof setInterval> | null = null

/** 数值格式化：缺失显示「—」（防误读账户归零），number 守卫对齐 TradesCard 教训。 */
function fmt(v: unknown): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(0) : '—'
}

/** 拉资产：失败静默保持上次值，避免轮询抖动导致数字闪跳。 */
async function fetchAsset() {
  try {
    asset.value = await getAsset()
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
