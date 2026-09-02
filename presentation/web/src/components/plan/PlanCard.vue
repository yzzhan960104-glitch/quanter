<!--
  PlanCard 每日计划卡（2026-09-02 首页）：今日实况 + 昨晚预演回看。

  物理意图：把"今天买了什么/为什么没买"放到首页第一屏——audit 驱动的
  信号→成交实况（状态 tag：已成交/在途/未挂），点击行开 K 线形态回放；
  头部挂预演对拍徽标（一致率）。前跑红线：明日预演不上公网（见快照注）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>每日计划 <span class="sub">· {{ doc?.today }}</span></span>
        <el-tag v-if="doc?.preview" size="small" :type="reconType" effect="plain">
          预演对拍 {{ doc.preview.recon.ok }}/{{ doc.preview.recon.total }}
        </el-tag>
        <el-tag v-else-if="doc" size="small" type="info" effect="plain">预演未生成</el-tag>
      </div>
    </template>
    <el-table :data="doc?.rows ?? []" size="small" @row-click="open"
              :empty-text="doc ? '今日无新信号（轮动空档日）' : '加载中…'">
      <el-table-column label="标的" min-width="150">
        <template #default="{ row }">
          <span class="sym">{{ row.sym }}</span><span class="name">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="数量" width="80">
        <template #default="{ row }">{{ row.qty ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="委托价" width="86">
        <template #default="{ row }">{{ row.price?.toFixed(2) ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="颈线" width="76">
        <template #default="{ row }">{{ row.neckline?.toFixed(2) ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="RR" width="60">
        <template #default="{ row }">{{ row.rr?.toFixed(1) ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="tagOf(row.status)" effect="plain">
            {{ labelOf(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
    </el-table>
    <template v-if="doc?.next_preview">
      <el-divider content-position="left">
        明日预演 · {{ doc.next_preview.plan_date }}（盘前参考）
      </el-divider>
      <el-table :data="doc.next_preview.rows" size="small" @row-click="open"
                empty-text="拟挂 0 单（无候选或全被闸拦）">
        <el-table-column label="标的" min-width="150">
          <template #default="{ row }">
            <span class="sym">{{ row.sym }}</span><span class="name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="qty" label="数量" width="80" />
        <el-table-column label="委托价" width="96">
          <template #default="{ row }">
            {{ row.entry?.toFixed(2) }}{{ row.clamped ? ' 钳' : '' }}
          </template>
        </el-table-column>
        <el-table-column label="颈线" width="76">
          <template #default="{ row }">{{ row.neckline?.toFixed(2) ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="RR" width="60">
          <template #default="{ row }">{{ row.rr?.toFixed(1) ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="形态日" width="96">
          <template #default="{ row }">{{ row.formed }}</template>
        </el-table-column>
      </el-table>
      <div v-if="doc.next_preview.blocked?.length || doc.next_preview.skip_held?.length"
           class="next-foot">
        <span v-for="b in doc.next_preview.blocked" :key="'b' + b.sym">
          拦截 · {{ b.sym }}：{{ b.why }}</span>
        <span v-for="k in doc.next_preview.skip_held" :key="'k' + k.sym">
          跳过 · {{ k.sym }}（{{ k.why === 'cooldown' ? '冷却' : '已持有' }}）</span>
      </div>
    </template>

    <div class="foot">
      <span v-if="doc">信号 {{ doc.summary.signals }} · 成交 {{ doc.summary.filled }}
        · 持仓跳过 {{ doc.summary.skip_held }} · 拦截 {{ doc.summary.blocked }}</span>
      <span class="sub">{{ doc?.note }}</span>
    </div>

    <el-drawer v-model="drawer" size="62%" :title="drawerTitle">
      <div v-if="kline" style="padding: 0 8px">
        <div class="kmeta">
          <span v-if="kline.marks?.rr">RR {{ kline.marks.rr }}</span>
          <span v-if="kline.marks?.formed_at">形态日 {{ kline.marks.formed_at }}</span>
          <span class="sub">数据截至 {{ kline.asof }} · 前复权</span>
        </div>
        <KlinePanel :data="kline" />
      </div>
      <el-empty v-else-if="drawer" description="该标的无 K 线快照" />
    </el-drawer>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getPlanCard, getOhlcv, type PlanCardDoc, type OhlcvData } from '../../api/home'
import KlinePanel from '../charts/KlinePanel.vue'

const doc = ref<PlanCardDoc | null>(null)
const drawer = ref(false)
const drawerTitle = ref('')
const kline = ref<OhlcvData | null>(null)

const reconType = computed(() => {
  const r = doc.value?.preview?.recon
  return !r || r.ok === r.total ? 'success' : 'warning'
})
const tagOf = (s: string) => (s === 'filled' ? 'success' : s === 'placed' ? 'warning' : 'info')
const labelOf = (s: string) => (s === 'filled' ? '已成交' : s === 'placed' ? '在途' : '未挂')

async function open(row: { sym: string; name: string }) {
  drawerTitle.value = `${row.sym} ${row.name} · K 线形态回放`
  drawer.value = true
  kline.value = await getOhlcv(row.sym).catch(() => null)
}

onMounted(async () => {
  doc.value = await getPlanCard().catch(() => null)
})
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.sym { font-family: var(--qt-font-mono, monospace); cursor: pointer; }
.name { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 8px; }
.foot { display: flex; justify-content: space-between; margin-top: 8px;
        font-size: 12px; color: var(--el-text-color-regular); flex-wrap: wrap; gap: 8px; }
.kmeta { display: flex; gap: 16px; margin-bottom: 8px; font-size: 13px; }
.next-foot { display: flex; flex-direction: column; gap: 2px; margin-top: 6px;
             font-size: 11px; color: var(--el-text-color-secondary); }
</style>
