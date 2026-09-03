<!--
  ProposalsView 研究提案流（全读可视化 P6.1 · 2026-09-03 · /research）。

  物理意图：策略怎么想出来的——提案生命线一屏：①状态管道看板（PUBLISHED/
  REJECTED 计数 + 每提案 verdict）②提案时间线表（假设/参数 diff/预期效果/
  风险/验证结论；相邻版本参数高亮=同键变更着色）。数据=proposals.json
  快照（research_proposals.db 只读镜像）。
-->
<template>
  <div class="research">
    <div class="head">
      <h2>研究提案流</h2>
      <div class="pipe">
        <span v-for="(n, s) in doc?.pipeline" :key="s" class="pipe-chip"
              :class="`st-${statusOfP(String(s)).type}`">
          {{ statusOfP(String(s)).label }} {{ n }}
        </span>
        <span class="sub">共 {{ doc?.rows.length || 0 }} 条 · {{ doc?.generated_at }}</span>
      </div>
    </div>

    <el-card shadow="never">
      <el-table :data="doc?.rows || []" size="small" v-loading="!doc"
                :empty-text="doc ? '暂无提案' : '加载中…'">
        <el-table-column label="提案日" width="96">
          <template #default="{ row }">{{ String(row.created_at || '').slice(5, 10) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <el-tag :type="statusOfP(row.status).type" size="small" effect="plain">
              {{ statusOfP(row.status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="假设" min-width="260">
          <template #default="{ row }">
            <span class="hyp">{{ row.hypothesis }}</span>
          </template>
        </el-table-column>
        <el-table-column label="参数" min-width="200">
          <template #default="{ row, $index }">
            <span v-for="(v, k) in row.params" :key="k" class="param"
                  :class="{ drift: isDrift(String(k), $index) }">
              {{ k }}={{ shortVal(v) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="验证结论" min-width="220">
          <template #default="{ row }">
            <el-tooltip v-if="row.verification?.reason" placement="top"
                        :content="JSON.stringify(row.verification)">
              <span class="verdict" :class="statusOfP(row.status).type === 'success' ? 'ok' : 'bad'">
                {{ verdictOf(row) }}
              </span>
            </el-tooltip>
            <span v-else class="sub">—</span>
          </template>
        </el-table-column>
        <el-table-column label="风险" min-width="180">
          <template #default="{ row }">
            <span class="risk">{{ row.risk }}</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="foot sub">{{ doc?.note }}</div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getProposals, type ProposalsDoc, type ProposalRow } from '../api/research'

const doc = ref<ProposalsDoc | null>(null)

/** 状态显式枚举（code-review J-12：非 PUBLISHED 一律红「已否决」是二元塌缩，
 *  未来新增 DRAFT/VERIFIED 等态会错标——未知态中性灰不猜）。 */
const STATUS_ZH: Record<string, { label: string; type: 'success' | 'danger' | 'info' }> = {
  PUBLISHED: { label: '已发布', type: 'success' },
  REJECTED: { label: '已否决', type: 'danger' },
  VERIFIED: { label: '已验证', type: 'info' },
  DRAFT: { label: '草稿', type: 'info' },
}
const statusOfP = (s: string) => STATUS_ZH[s] ?? { label: s, type: 'info' }

const shortVal = (v: unknown): string => {
  const s = String(v)
  return s.length > 12 ? s.slice(0, 10) + '…' : s
}

function verdictOf(row: ProposalRow): string {
  const v = row.verification as { verdict?: string; reason?: string } | null
  return String(v?.reason || v?.verdict || '—')
}

/** 参数 diff 高亮：与上一行（时间序相邻提案）同键值不同 → 着色（变更点）。 */
function isDrift(key: string, index: number): boolean {
  const rows = doc.value?.rows ?? []
  const cur = rows[index]?.params as Record<string, unknown> | null
  const prev = rows[index + 1]?.params as Record<string, unknown> | null
  if (!cur || !prev || !(key in prev)) return false
  return String(prev[key]) !== String(cur[key])
}

onMounted(async () => {
  doc.value = await getProposals().catch(() => null)
})
</script>

<style scoped>
.research { flex: 1; overflow-y: auto; width: 100%;
            padding: var(--qt-space-3, 16px); background: var(--qt-bg-page); }
.head { display: flex; align-items: center; justify-content: space-between;
        margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
.head h2 { margin: 0; }
.pipe { display: flex; align-items: center; gap: 8px; }
.pipe-chip { font-size: 12px; padding: 2px 10px; border-radius: 10px; }
.st-success { color: #2eaf62; background: rgba(46, 175, 98, 0.1); }
.st-danger { color: #ef5350; background: rgba(239, 83, 80, 0.08); }
.st-info { color: var(--el-text-color-secondary); background: rgba(134, 144, 156, 0.1); }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
.hyp { font-size: 12px; line-height: 1.5; }
.param { display: inline-block; font-family: var(--qt-font-mono, monospace);
         font-size: 11px; margin: 1px 8px 1px 0; color: var(--el-text-color-regular); }
.param.drift { color: #c2502a; font-weight: 700;
               background: rgba(194, 80, 42, 0.08); border-radius: 3px; padding: 0 3px; }
.verdict.ok { color: #2eaf62; }
.verdict.bad { color: #ef5350; }
.risk { font-size: 12px; color: var(--el-text-color-secondary); }
.foot { margin-top: 8px; }
</style>
