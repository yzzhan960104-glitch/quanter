<!--
  HomeView 公网首页（可视化重构 P1 · 2026-09-01）。

  物理意图：访客 3 秒看懂——这是什么系统（颈线法量化·只读观测）、赚不赚
  （净值曲线+日历）、今天什么状态（双腿净值卡）。纯静态快照数据；公开站
  首屏即本页（静态态路由 '/'）。
-->
<template>
  <div class="home">
    <div class="hero">
      <div class="hero-title">
        <h1>Quanter <span class="thin">· 颈线法量化观测</span></h1>
        <p class="hero-sub">
          掘金终端双腿实盘（主腿=R6-8 冠军 · 实验腿=R10 候选对照）——
          本站为只读快照（每交易日定时发布），写入通路在结构上不存在。
        </p>
      </div>
      <div class="stat-cards">
        <div v-for="s in stats" :key="s.label" class="stat-card">
          <div class="stat-label">{{ s.label }}</div>
          <div class="stat-nav">{{ s.nav }}</div>
          <div class="stat-pnl" :class="(s.pnlPct ?? 0) >= 0 ? 'up' : 'down'">
            {{ s.pnlPct == null ? '—' : `${s.pnlPct >= 0 ? '+' : ''}${s.pnlPct}%` }}
            <span class="stat-scope">{{ s.scope }}</span>
          </div>
        </div>
      </div>
    </div>

    <PlanCard />

    <div style="margin-top: 12px">
      <NavCurve :history="history" :benchmarks="benchmarks" />
    </div>

    <div style="margin-top: 12px">
      <AllocationCard />
    </div>

    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :span="14"><PnlCalendar :history="history" /></el-col>
      <el-col :span="10">
        <el-card shadow="never" class="links-card">
          <template #header>深入观测</template>
          <router-link to="/cockpit" class="link">
            <span>综合看板</span><span class="sub">持仓 · 资金 · 流水 · K线回放</span>
          </router-link>
          <router-link to="/experiments" class="link">
            <span>实验对照</span><span class="sub">双腿 A/B · 轮次 · 事件流</span>
          </router-link>
          <router-link to="/data" class="link">
            <span>数据湖</span><span class="sub">43 数据集健康度</span>
          </router-link>
          <router-link to="/opportunity" class="link">
            <span>机会观察</span><span class="sub">紫金×纽约金 · 美元×US10Y×金</span>
          </router-link>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getNavHistory, getBenchmarks, type NavHistory, type Benchmarks } from '../api/home'
import NavCurve from '../components/charts/NavCurve.vue'
import PnlCalendar from '../components/charts/PnlCalendar.vue'
import AllocationCard from '../components/charts/AllocationCard.vue'
import PlanCard from '../components/plan/PlanCard.vue'

const history = ref<NavHistory>({ base: 200000, era_start: '', days: [] })
const benchmarks = ref<Benchmarks | null>(null)

const stats = computed(() => {
  const days = history.value.days ?? []
  const last = days[days.length - 1]
  const prev = days[days.length - 2]
  const mk = (label: string, leg: string) => {
    const nav = last?.legs?.[leg]
    const prevNav = prev?.legs?.[leg] ?? history.value.base
    const pnlPct = nav != null && prevNav
      ? +(((nav / prevNav) - 1) * 100).toFixed(2) : null
    return {
      label, nav: nav != null ? nav.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '—',
      pnlPct, scope: prev ? '今日' : '较入金',
    }
  }
  return [mk('主腿净值', 'main'), mk('实验腿净值', 'exp')]
})

onMounted(async () => {
  try {
    history.value = await getNavHistory()
  } catch { /* 快照缺失：图表组件自降级占位 */ }
  benchmarks.value = await getBenchmarks().catch(() => null)
})
</script>

<style scoped>
.home { flex: 1; overflow-y: auto; width: 100%; padding: var(--qt-space-3, 16px); max-width: 1200px; margin: 0 auto;
        background: var(--qt-bg-page); min-height: 100%; }
.hero { display: flex; justify-content: space-between; align-items: flex-end;
        gap: 24px; flex-wrap: wrap; margin-bottom: 16px; }
.hero-title h1 { margin: 0; font-size: 26px; }
.hero-title .thin { font-weight: 300; font-size: 18px; color: var(--el-text-color-secondary); }
.hero-sub { color: var(--el-text-color-secondary); font-size: 13px; margin: 8px 0 0; max-width: 560px; }
.stat-cards { display: flex; gap: 12px; }
.stat-card { background: var(--qt-bg-card, #ffffff); border: 1px solid var(--qt-border, #dcdfe6);
             border-radius: 8px; padding: 12px 18px; min-width: 150px; }
.stat-label { color: var(--el-text-color-secondary); font-size: 12px; }
.stat-nav { font-size: 22px; font-weight: 600; margin: 4px 0; }
.stat-pnl { font-size: 14px; }
.stat-scope { color: var(--el-text-color-secondary); font-size: 11px; margin-left: 4px; }
.up { color: #ef5350; }
.down { color: #26a69a; }
.links-card .link { display: flex; justify-content: space-between; align-items: baseline;
                    padding: 10px 4px; border-bottom: 1px solid var(--qt-border, #dcdfe6);
                    color: var(--el-text-color-primary); text-decoration: none; }
.links-card .link:last-child { border-bottom: none; }
.links-card .link .sub { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
