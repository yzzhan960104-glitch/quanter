<!--
  LoserReviewCard 亏损持仓 LLM 归因卡（2026-09-03 · ops/loser_review 产物）。

  物理意图：浮亏持仓「为什么亏」的每日深度归因——每只：头部（名称/浮亏%/
  持有天数/距止损/trailing 状态 + 归因主因一句 + 风险状态三档 tag）+ 可展开
  markdown 正文（亏损过程/归因展开/状态判定依据）。模型 glm-5.3（页脚注记）。
  红线：risk_state 三档=状态描述（持有观察/收紧关注/临近风控线），不是交易
  指令；分析失败行降级显示 error。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>亏损归因 <span class="sub">（{{ legLabel }} · 浮亏 {{ rows.length }} 只）</span></span>
        <span class="sub">{{ doc?.model || '—' }} · {{ doc?.generated_at || '' }}</span>
      </div>
    </template>
    <div v-if="rows.length" class="lv-list">
      <div v-for="(r, i) in rows" :key="r.symbol" class="lv-item">
        <div class="lv-head" @click="toggle(i)">
          <span class="lv-sym mono">{{ r.symbol }}</span>
          <span class="lv-name">{{ r.name }}</span>
          <span class="lv-pct down">{{ r.fpnl_pct }}%</span>
          <span class="lv-meta sub">持 {{ r.days_held ?? '—' }}/{{ r.max_holding }}日
            · 距止损 {{ r.dist_stop_pct ?? '—' }}%
            · {{ r.trailing?.in_grace ? 'grace 内' : `trailing 已收紧${r.trailing?.steps_taken ?? 0}步` }}</span>
          <span v-if="r.analysis?.risk_state" class="lv-state"
                :class="stateCls(r.analysis.risk_state)">{{ r.analysis.risk_state }}</span>
          <el-tag v-else size="small" effect="plain" type="info">未判定</el-tag>
          <span class="lv-primary">{{ r.analysis?.primary || r.analysis?.error || '—' }}</span>
          <span class="lv-toggle">{{ open.has(i) ? '▾' : '▸' }}</span>
        </div>
        <div v-if="open.has(i)" class="lv-body">
          <div class="lv-facts">
            <span>进场 {{ r.entry_date?.slice(5) }} @ {{ r.entry_price?.toFixed(2) }}</span>
            <span>信号颈线 {{ r.signal?.neckline ?? '—' }} · RR {{ r.signal?.rr ?? '—' }}</span>
            <span>高点回撤 {{ r.kline_summary?.dd_pct ?? '—' }}%</span>
            <span>同期上证 {{ r.market?.sh_pct ?? '—' }}%</span>
          </div>
          <div v-if="r.analysis?.evidence" class="lv-evidence">
            证据：{{ r.analysis.evidence }}（置信度 {{ r.analysis.confidence || '—' }}）
          </div>
          <pre v-if="r.analysis?.markdown" class="lv-md">{{ r.analysis.markdown }}</pre>
          <div v-else class="sub">分析正文缺失（{{ r.analysis?.error || 'LLM 输出解析降级' }}）</div>
        </div>
      </div>
    </div>
    <el-empty v-else :description="doc ? '当日无浮亏持仓（全绿）' : '归因快照缺失'"
              :image-size="50" />
    <div class="foot sub">{{ doc?.note }}</div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getLoserReview, type LoserReviewDoc } from '../../api/review'

const props = defineProps<{ leg?: string }>()
const injected = ref('main')
// 与 cockpit 组件族同源：显式 prop 优先，宿主未传时吃 cockpit-leg 注入
import { inject, type Ref } from 'vue'
const injectedLeg = inject<Ref<string>>('cockpit-leg', injected)
const effLeg = computed(() => props.leg ?? injectedLeg.value)

const doc = ref<LoserReviewDoc | null>(null)
const open = ref(new Set<number>())

const rows = computed(() =>
  doc.value?.legs?.find((l) => l.leg === effLeg.value)?.rows ?? [])
const legLabel = computed(() =>
  doc.value?.legs?.find((l) => l.leg === effLeg.value)?.label
  || (effLeg.value === 'exp' ? '实验腿' : '主腿'))

function toggle(i: number) {
  const s = new Set(open.value)
  s.has(i) ? s.delete(i) : s.add(i)
  open.value = s
}

const stateCls = (s: string) =>
  s === '临近风控线' ? 'st-danger' : s === '收紧关注' ? 'st-warn' : 'st-ok'

onMounted(async () => {
  doc.value = await getLoserReview().catch(() => null)
})
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.mono { font-family: var(--qt-font-mono, monospace); }
.lv-list { display: flex; flex-direction: column; gap: 6px; }
.lv-item { border: 1px solid var(--qt-border, #dcdfe6); border-radius: 6px; }
.lv-head { display: flex; align-items: center; gap: 10px; padding: 8px 12px;
           cursor: pointer; flex-wrap: wrap; font-size: 13px; }
.lv-head:hover { background: var(--qt-bg-overlay, #f5f7fa); }
.lv-sym { font-weight: 600; }
.lv-name { color: var(--el-text-color-secondary); }
.lv-pct { font-weight: 700; }
.down { color: #26a69a; }                      /* A 股语义：亏=绿 */
.lv-meta { flex: 1; min-width: 200px; }
.lv-state { font-size: 12px; padding: 1px 10px; border-radius: 10px; white-space: nowrap; }
.st-ok { color: #2eaf62; background: rgba(46, 175, 98, 0.1); }
.st-warn { color: var(--qt-warn, #b88230); background: rgba(184, 130, 48, 0.12); }
.st-danger { color: #fff; background: #ef5350; font-weight: 700; }
.lv-primary { font-size: 12px; color: var(--el-text-color-regular);
              max-width: 420px; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }
.lv-toggle { color: var(--el-text-color-secondary); }
.lv-body { padding: 4px 12px 10px; border-top: 1px dashed var(--qt-border, #dcdfe6); }
.lv-facts { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px;
            color: var(--el-text-color-secondary); padding: 6px 0; }
.lv-evidence { font-size: 12px; color: var(--el-text-color-regular); padding: 2px 0; }
.lv-md { white-space: pre-wrap; word-break: break-word; font-family: inherit;
         font-size: 13px; line-height: 1.7; margin: 6px 0 0;
         color: var(--el-text-color-primary); }
.foot { margin-top: 8px; }
</style>
