<!--
  CommitteeReviewPanel 委员会评审附签面板（每日回顾页 · 2026-09-07）。

  物理意图：把 research/committee 的评审产物（verdict/知识库冲突/事实核查/
  证据等级/备注）以统一形态挂在「明日计划」与「亏损归因」下方——评审是
  假设与观点的质证，不是交易指令（disclaimer 常驻页脚）。

  数据形状=api/review.ts CommitteeReview（loser_review 与 plan_preview 两处
  同形，一套渲染）；缺省（旧档案无评审）→ 折叠占位，不占版面。
-->
<template>
  <el-card v-if="review" shadow="never" class="cr-panel">
    <template #header>
      <div class="flex-between">
        <span>委员会评审 <span class="sub" v-if="review.tier">· Tier {{ review.tier }}</span></span>
        <div class="tags">
          <el-tag v-if="review.llm_calls != null" size="small" type="info" effect="plain">
            LLM×{{ review.llm_calls }}
          </el-tag>
          <el-tag v-if="review.evidence_grade" size="small" type="info" effect="plain">
            证据 {{ review.evidence_grade }}
          </el-tag>
          <el-tag size="small" :type="verdictType" effect="dark">{{ review.verdict }}</el-tag>
        </div>
      </div>
    </template>

    <div v-if="guards?.t1" class="guard-banner" :class="{ blocked: guards.t1.blocked }">
      <b>均衡栈守卫预判</b>（{{ guards.index }} @ {{ guards.t1.as_of }}）：
      收盘 {{ guards.t1.close }} vs MA{{ guards.ma_window }} {{ guards.t1.ma }}
      （ratio {{ guards.t1.ratio }}）→
      <b>{{ guards.t1.balanced_action || '不可判' }}</b>
    </div>
    <div v-else-if="guards" class="guard-banner muted">
      <b>均衡栈守卫预判</b>：{{ guards.t1_error || '指数数据不可判（fail-open）' }}
    </div>

    <p v-if="review.notes" class="cr-notes">{{ review.notes }}</p>

    <div v-if="kbConflicts.length" class="cr-block">
      <div class="cr-block-title">知识库冲突（{{ kbConflicts.length }}）</div>
      <ul>
        <li v-for="(c, i) in kbConflicts" :key="'kb' + i">{{ typeof c === 'string' ? c : JSON.stringify(c) }}</li>
      </ul>
    </div>
    <div v-if="tier0Conflicts.length" class="cr-block">
      <div class="cr-block-title">Tier 0 谱面闸（{{ tier0Conflicts.length }}）</div>
      <ul>
        <li v-for="(c, i) in tier0Conflicts" :key="'t0' + i">{{ c }}</li>
      </ul>
    </div>
    <div v-if="factChecks.length" class="cr-block">
      <div class="cr-block-title">事实核查（{{ factChecks.length }}）</div>
      <ul>
        <li v-for="(f, i) in factChecks" :key="'fc' + i" :class="{ ok: factOk(f), bad: !factOk(f) }">
          {{ factText(f) }}
        </li>
      </ul>
    </div>

    <div class="cr-foot" v-if="review.disclaimer">{{ review.disclaimer }}</div>
  </el-card>
  <el-card v-else-if="placeholder" shadow="never" class="cr-panel">
    <div class="cr-empty">评审未生成（旧档案 / 评审链未运行）</div>
  </el-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { CommitteeReview } from '../../api/review'
import type { PlanGuards } from '../../api/home'

const props = defineProps<{
  review?: CommitteeReview | null
  guards?: PlanGuards | null
  /** true=无评审也渲染占位（明日计划块常驻）；归因块跟随评审有无。 */
  placeholder?: boolean
}>()

const verdictType = computed(() => {
  const v = props.review?.verdict
  if (v === 'PASS') return 'success'
  if (v === 'NOTES') return 'warning'
  if (v === 'ESCALATE') return 'danger'
  return 'info'
})
const kbConflicts = computed(() => (props.review?.kb_conflicts ?? []) as string[])
const tier0Conflicts = computed(() => (props.review?.tier0_conflicts ?? []) as string[])
const factChecks = computed(() => props.review?.fact_checks ?? [])
const factOk = (f: unknown) => typeof f === 'object' && f !== null ? (f as { ok?: boolean }).ok !== false : true
const factText = (f: unknown) =>
  typeof f === 'string' ? f
    : `${(f as { claim?: string }).claim ?? ''} — ${(f as { note?: string }).note ?? ''}`
</script>

<style scoped>
.cr-panel :deep(.el-card__body) { padding: 12px 16px; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 6px; }
.tags { display: flex; gap: 6px; align-items: center; }
.guard-banner {
  padding: 8px 12px; border-radius: 6px; margin-bottom: 10px; font-size: 13px;
  background: var(--el-fill-color-light);
}
.guard-banner.blocked { background: var(--el-color-danger-light-9); }
.guard-banner.muted { color: var(--el-text-color-secondary); }
.cr-notes { margin: 6px 0; font-size: 13px; line-height: 1.7; white-space: pre-wrap; }
.cr-block { margin: 8px 0; }
.cr-block-title { font-size: 12px; color: var(--el-text-color-secondary); margin-bottom: 4px; }
.cr-block ul { margin: 0; padding-left: 18px; font-size: 12.5px; line-height: 1.8; }
.cr-block li.ok::before { content: '✓ '; color: var(--el-color-success); }
.cr-block li.bad::before { content: '✗ '; color: var(--el-color-danger); }
.cr-foot { margin-top: 8px; font-size: 11.5px; color: var(--el-text-color-secondary);
  border-top: 1px dashed var(--el-border-color); padding-top: 6px; }
.cr-empty { color: var(--el-text-color-secondary); font-size: 13px; padding: 8px 0; }
</style>
