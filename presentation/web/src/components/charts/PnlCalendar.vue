<!--
  PnlCalendar 日收益日历（2026-09-02 富途样式重做）。

  物理意图：GitHub 热力图 → 富途盈亏日历形态——月历网格内直接读数：
  每格=日期+当日盈亏金额（A 股红涨绿跌），底色浓度按 |盈亏| 相对月内极值
  渐变；周末/无交易日淡化；头部月合计（Σ日盈亏 + 月收益率）；‹ › 翻月。
  纯 CSS 网格自绘（ECharts calendar 的科研热力风格不适合读数，弃用）。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <div class="title">
          <span>日收益日历</span>
          <span class="month-sum" :class="(monthSum?.pnl ?? 0) >= 0 ? 'up' : 'down'">
            {{ monthSum ? fmtAmt(monthSum.pnl) : '—' }}
            <em v-if="monthSum">{{ monthSum.pct >= 0 ? '+' : '' }}{{ monthSum.pct }}%</em>
          </span>
        </div>
        <div class="ctrl">
          <el-button size="small" text @click="offset--">‹</el-button>
          <span class="month">{{ monthLabel }}</span>
          <el-button size="small" text :disabled="offset >= 0" @click="offset++">›</el-button>
          <el-select v-model="leg" size="small" style="width: 96px">
            <el-option label="主腿" value="main" />
            <el-option label="实验腿" value="exp" />
          </el-select>
        </div>
      </div>
      <div class="week-row">
        <span v-for="w in ['一','二','三','四','五','六','日']" :key="w">{{ w }}</span>
      </div>
    </template>

    <div class="cal-grid">
      <div v-for="(c, i) in cells" :key="i" class="cell"
           :class="{ blank: !c, weekend: c?.weekend, today: c?.today }"
           :style="bgOf(c)">
        <template v-if="c">
          <span class="d">{{ c.day }}</span>
          <template v-if="c.pnl != null">
            <span class="pnl" :class="c.pnl >= 0 ? 'up' : 'down'">
              {{ c.pnl >= 0 ? '+' : '' }}{{ Math.round(c.pnl) }}
            </span>
            <span class="pct" :class="c.pnl >= 0 ? 'up' : 'down'">{{ c.pct }}%</span>
          </template>
        </template>
      </div>
    </div>
    <div v-if="!cells.some((c) => c?.pnl != null)" class="empty">本月暂无交易数据</div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { NavHistory, NavDay } from '../../api/home'

const props = defineProps<{ history: NavHistory }>()
const leg = ref<'main' | 'exp'>('main')
const offset = ref(0)

/** 基月=最新数据所在月；offset 翻月（≥0 禁未来）。 */
const baseMonth = computed(() => {
  const ds = props.history.days ?? []
  if (ds.length) return ds[ds.length - 1].date.slice(0, 7)
  const n = new Date()
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}`
})
const monthRange = computed(() => {
  const [y, m] = baseMonth.value.split('-').map(Number)
  const d = new Date(y, m - 1 + offset.value, 1)
  return { y: d.getFullYear(), m: d.getMonth() }
})
const monthLabel = computed(() =>
  `${monthRange.value.y} 年 ${monthRange.value.m + 1} 月`)

interface Cell { day: number; weekend: boolean; today: boolean
  pnl: number | null; pct: number | null; nav: number | null }

/** 日盈亏（金额=nav 差，%=日收益率；首日较入金基数）。 */
function dailyOf(): Map<string, { pnl: number; pct: number; nav: number }> {
  const out = new Map<string, { pnl: number; pct: number; nav: number }>()
  let prev: number | null = null
  for (const d of props.history.days ?? []) {
    const nav = d.legs?.[leg.value]
    if (nav == null) { prev = null; continue }
    const anchor = prev ?? props.history.base
    out.set(d.date, {
      pnl: prev != null ? +(nav - prev).toFixed(0) : 0,
      pct: +((nav / anchor - 1) * 100).toFixed(2),
      nav,
    })
    prev = nav
  }
  return out
}

const daily = computed(dailyOf)

/** 月内格序列（含前置空位对齐周一起始）。 */
const cells = computed<Array<Cell | null>>(() => {
  const { y, m } = monthRange.value
  const first = new Date(y, m, 1)
  const days = new Date(y, m + 1, 0).getDate()
  const lead = (first.getDay() + 6) % 7              // 周一=0
  const todayStr = new Date().toISOString().slice(0, 10)
  const out: Array<Cell | null> = Array(lead).fill(null)
  for (let day = 1; day <= days; day++) {
    const ds = `${y}-${String(m + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
    const wd = new Date(y, m, day).getDay()
    const rec = daily.value.get(ds)
    out.push({
      day, weekend: wd === 0 || wd === 6, today: ds === todayStr,
      pnl: rec?.pnl ?? null, pct: rec?.pct ?? null, nav: rec?.nav ?? null,
    })
  }
  return out
})

const maxAbs = computed(() =>
  Math.max(1, ...cells.value.map((c) => Math.abs(c?.pnl ?? 0))))

/** 底色浓度：|盈亏|/月内极值 × 32% 上限（红/绿半透明，富途式柔和渐变）。 */
function bgOf(c: Cell | null) {
  if (!c || c.pnl == null || c.pnl === 0) return {}
  const a = Math.min(0.32, Math.abs(c.pnl) / maxAbs.value * 0.32)
  return { background: c.pnl > 0 ? `rgba(239,83,80,${a})` : `rgba(38,166,154,${a})` }
}

/** 月合计（Σ日盈亏 + 月收益率=月末 nav/上月末-1，首月较基数）。 */
const monthSum = computed(() => {
  const { y, m } = monthRange.value
  const inMonth = (props.history.days ?? []).filter((d: NavDay) =>
    d.date.startsWith(`${y}-${String(m + 1).padStart(2, '0')}`))
  const navs = inMonth.map((d) => d.legs?.[leg.value]).filter((v): v is number => v != null)
  if (!navs.length) return null
  const last = navs[navs.length - 1]
  const prevMonthKey = m === 0 ? `${y - 1}-12` : `${y}-${String(m).padStart(2, '0')}`
  const prevNav = [...(props.history.days ?? [])].reverse()
    .find((d) => d.date.startsWith(prevMonthKey) && d.legs?.[leg.value] != null)
    ?.legs?.[leg.value] ?? props.history.base
  // Σ日盈亏：首日不计（其 pnl=0），其余=当日 nav − 前有 nav
  let sum = 0, prev: number | null = null
  for (const d of props.history.days ?? []) {
    const nav = d.legs?.[leg.value]
    if (nav == null) { prev = null; continue }
    if (d.date.startsWith(`${y}-${String(m + 1).padStart(2, '0')}`) && prev != null) {
      sum += nav - prev
    }
    prev = nav
  }
  return { pnl: +sum.toFixed(0), pct: +((last / prevNav - 1) * 100).toFixed(2) }
})

const fmtAmt = (v: number) =>
  `${v >= 0 ? '+' : ''}${v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
</script>

<style scoped>
.flex-between { display: flex; align-items: center; justify-content: space-between; }
.title { display: flex; align-items: baseline; gap: 12px; }
.month-sum { font-size: 15px; font-weight: 600; }
.month-sum em { font-style: normal; font-size: 12px; font-weight: 400; opacity: 0.85; }
.ctrl { display: flex; align-items: center; gap: 4px; }
.month { font-size: 12px; color: var(--el-text-color-regular); min-width: 76px; text-align: center; }
.week-row { display: grid; grid-template-columns: repeat(7, 1fr); margin-top: 10px; }
.week-row span { text-align: center; font-size: 11px; color: var(--el-text-color-secondary); }
.cal-grid { display: grid; grid-template-columns: repeat(7, 1fr);
            gap: 4px; margin-top: 4px; }
.cell { aspect-ratio: 1 / 0.72; border-radius: 8px; padding: 4px 6px;
        background: var(--qt-bg-overlay, #f0f2f5);
        display: flex; flex-direction: column; justify-content: space-between;
        min-height: 52px; transition: transform .1s; }
.cell:hover { transform: scale(1.04); }
.cell.blank { background: transparent; }
.cell.weekend { opacity: 0.45; }
.cell.today { outline: 1.5px solid var(--qt-accent, #2962ff); }
.d { font-size: 10px; color: var(--el-text-color-secondary); }
.pnl { font-size: 13px; font-weight: 700; line-height: 1.1;
       font-family: var(--qt-font-mono, monospace); }
.pct { font-size: 10px; line-height: 1; font-family: var(--qt-font-mono, monospace); }
.up { color: #ef5350; }
.down { color: #26a69a; }
.empty { color: var(--el-text-color-secondary); font-size: 12px;
         text-align: center; padding: 18px 0 6px; }
</style>
