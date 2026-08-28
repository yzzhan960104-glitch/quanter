<!--
  AbDailyCard 当日对照卡（2026-08-29 多腿方案 §3.2 · ExperimentView）。

  物理意图:/gm/ab 结构化负载的可视化——① 两腿工程闸漏斗对照行 ② 信号 diff 表
  (校准轮预期空) ③ 订单结构 diff + qty 差(资金口径预期) ④ 红旗区。

  认识论红线:只展示工程闸与结构 diff;仿真撮合的收益对比永不进本卡
  (R7c 先知调度教训——仿真读数不构成决策证据,Web 不制造诱导性画面)。

  数据时机:ab 数据由 15:40 ingest 落库、15:50 对照产出——盘中/盘前显示
  "今日数据 15:50 后更新"占位,--date 可回看历史(父组件传 date)。
-->
<template>
  <el-card shadow="never" class="ab-card">
    <template #header>
      <div class="flex-between">
        <span>当日对照 · {{ date }}</span>
        <el-tag v-if="payload" :type="payload.ok ? 'success' : 'danger'" size="small">
          {{ payload.ok ? '一致 ✅' : `${payload.flags?.length ?? 0} 红旗` }}
        </el-tag>
      </div>
    </template>

    <div v-if="!payload" class="ab-empty">加载中…</div>
    <div v-else-if="payload.single_leg" class="ab-empty">{{ payload.detail }}</div>
    <template v-else>
      <!-- ① 工程闸漏斗对照（行=事件,列=腿） -->
      <el-table :data="gateRows" size="small" class="gate-table">
        <el-table-column prop="event" label="事件" width="140" />
        <el-table-column prop="main" label="主腿" />
        <el-table-column prop="exp" label="实验腿" />
      </el-table>
      <!-- ② 信号 diff -->
      <div class="sec-title">信号对照（校准轮预期 diff=0）</div>
      <div v-if="hasSignalDiff" class="diff-body">
        <div v-if="payload.signals!.added.length">新增: {{ payload.signals!.added.join(' ') }}</div>
        <div v-if="payload.signals!.removed.length">消失: {{ payload.signals!.removed.join(' ') }}</div>
        <div v-for="d in payload.signals!.drifted" :key="d.symbol">
          漂移: {{ d.symbol }} {{ d.incumbent }} → {{ d.challenger }}
        </div>
      </div>
      <div v-else class="diff-body ok-line">完全一致（{{ payload.main?.counts?.SIGNAL ?? 0 }} 信号）</div>
      <!-- ③ 订单 qty 差（预期形态） -->
      <div v-if="payload.orders?.qty_diff?.length" class="sec-title">qty 差（资金口径,预期）</div>
      <div v-if="payload.orders?.qty_diff?.length" class="diff-body">
        <span v-for="q in payload.orders.qty_diff" :key="q.symbol" class="qty-chip">
          {{ q.symbol }} {{ q.incumbent }}→{{ q.challenger }}
        </span>
      </div>
      <!-- ④ 红旗区 -->
      <div v-if="payload.flags?.length" class="flags">
        <div v-for="(f, i) in payload.flags" :key="i" class="flag-line">🔴 {{ f }}</div>
      </div>
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { getAbDaily, type AbDaily } from '../../api/gm'

const props = defineProps<{ date: string }>()
const payload = ref<AbDaily | null>(null)

const GATE_EVENTS = ['SCHEDULE_TICK', 'SIGNAL', 'ORDER_PLACED', 'ORDER_BLOCKED', 'EOD']
const gateRows = computed(() =>
  GATE_EVENTS.map((ev) => ({
    event: ev,
    main: payload.value?.main?.counts?.[ev] ?? 0,
    exp: payload.value?.exp?.counts?.[ev] ?? 0,
  })))

const hasSignalDiff = computed(() => {
  const s = payload.value?.signals
  return !!s && (s.added.length > 0 || s.removed.length > 0 || s.drifted.length > 0)
})

async function load() {
  payload.value = null
  try {
    payload.value = await getAbDaily(props.date)
  } catch {
    payload.value = { day: props.date, single_leg: true, detail: '对照数据不可达（server/台账库）' }
  }
}

watch(() => props.date, load)
onMounted(load)

defineExpose({ payload, load })
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.ab-empty { color: var(--qt-text-secondary); font-size: var(--qt-fs-caption); padding: var(--qt-space-2) 0; }
.gate-table { margin-bottom: var(--qt-space-2); }
.sec-title { font-size: var(--qt-fs-caption); color: var(--qt-text-secondary); margin: var(--qt-space-2) 0 4px; }
.diff-body { font-size: var(--qt-fs-caption); font-family: var(--qt-font-mono); line-height: 1.7; }
.ok-line { color: var(--qt-text-secondary); }
.qty-chip { margin-right: var(--qt-space-3); }
.flags { margin-top: var(--qt-space-2); border-top: 1px solid var(--qt-border, #333); padding-top: var(--qt-space-2); }
.flag-line { color: var(--qt-up); font-size: var(--qt-fs-caption); line-height: 1.7; }
</style>
