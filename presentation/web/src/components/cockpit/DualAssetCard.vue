<!--
  DualAssetCard 双腿资金并排卡（2026-08-29 cockpit 多腿方案 §3.1，替代单腿 AssetCard）。

  物理意图：A/B 对照的核心视觉——主腿/实验腿的 nav/available 并排两列，一眼对照
  两国度资金水位。每腿独立轮询 GET /api/v1/gm/asset?leg=<key>；单腿失败只降级该腿
  （显示「—」），另一腿照常——7002 查询按账户隔离，一腿断连不应拖瞎另一腿。

  数值边界守护（沿 AssetCard 虚假繁荣防线）：
    - 字段缺失/查询失败 → 「—」而非 0（防误读账户归零）；
    - 轮询失败保持上次值：单次抖动不让数字闪跳；
    - 实验腿未部署（/legs 只回 main）→ 右列显示占位态而非报错。
-->
<template>
  <el-card shadow="never">
    <template #header><span>资金资产（双腿对照）</span></template>
    <div class="dual-grid">
      <div v-for="leg in legs" :key="leg.key" class="leg-col" :class="{ dim: leg.missing }">
        <div class="leg-head">
          <span class="leg-tag" :class="leg.key">{{ leg.label }}</span>
          <span class="leg-name">{{ leg.name }}</span>
        </div>
        <div class="asset-grid">
          <div class="asset-item">
            <div class="asset-label">总权益</div>
            <div class="asset-value">{{ fmt(leg.asset.nav) }}</div>
          </div>
          <div class="asset-item">
            <div class="asset-label">可用资金</div>
            <div class="asset-value">{{ fmt(leg.asset.available) }}</div>
          </div>
        </div>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { reactive, onMounted, onUnmounted } from 'vue'
import { getLegs, getAssetByLeg, type GmAsset } from '../../api/gm'

interface LegView {
  key: string
  label: string
  name: string
  asset: GmAsset
  missing: boolean       // 未部署（/legs 缺该腿）→ 占位态
}

const legs = reactive<LegView[]>([])
let timer: ReturnType<typeof setInterval> | null = null

function fmt(v: unknown): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(0) : '—'
}

/** 拉全部在役腿资产：每腿独立 try（一腿断连不拖瞎另一腿），失败保持上次值。 */
async function fetchAll() {
  try {
    const catalog = await getLegs()
    const seen = new Set<string>()
    for (const l of catalog) {
      seen.add(l.key)
      let existing = legs.find((x) => x.key === l.key)
      if (!existing) {
        legs.push({ key: l.key, label: l.label, name: l.strategy_name || l.key, asset: {}, missing: false })
        existing = legs[legs.length - 1]        // 首轮即拉（否则首屏 5s 恒「—」）
      }
      if (!existing.missing) {
        try {
          existing.asset = await getAssetByLeg(l.key)
        } catch { /* 该腿抖动：保持上次值 */ }
      }
    }
    legs.forEach((x) => { if (!seen.has(x.key)) x.missing = true })
  } catch { /* /legs 不可达：整体保持上次（与 AssetCard 同款静默策略） */ }
}

onMounted(() => {
  fetchAll()
  timer = setInterval(fetchAll, 5000)
})

onUnmounted(() => {
  if (timer) { clearInterval(timer); timer = null }
})

defineExpose({ legs, fetchAll })
</script>

<style scoped>
.dual-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--qt-space-3);
}
.leg-col.dim { opacity: 0.45; }
.leg-head {
  display: flex;
  align-items: center;
  gap: var(--qt-space-2);
  margin-bottom: var(--qt-space-2);
}
.leg-tag {
  font-size: var(--qt-fs-caption);
  padding: 1px 8px;
  border-radius: 3px;
  background: var(--qt-panel-2, #2a2f3a);
  color: var(--qt-text-secondary);
}
.leg-tag.exp { color: var(--qt-warn); }
.leg-name {
  font-size: var(--qt-fs-caption);
  color: var(--qt-text-secondary);
}
.asset-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--qt-space-3);
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
