<script setup lang="ts">
/**
 * 应用根壳（路由导航 + 出口）
 *
 * 职责：
 *   1. 顶部导航条：在 4 个功能页间切换（图标 + 文字双标，按使用动线分组）
 *   2. <router-view/> 渲染当前路由对应的视图
 *
 * 导航信息架构（蔡森形态学流水线 Phase 3 Task 8 起）：
 * - 左段「研究/配置」4 项：蔡森筛选（形态学流水线，研究第一入口）→ 宏观驾驶舱 →
 *   数据湖 → AI 复盘（按研究动线：形态学选股 → 宏观面 → 数据资产 → 复盘诊断）。
 *   Phase 3 建 CaisenScreenView 后首页改指 /caisen，蔡森筛选作为研究/配置首项。
 * - 右段「实盘」1 项：实盘中控。用 .nav-divider 细分隔线物理区隔——这是全站唯一会
 *   真实下单的高危入口，空间区隔降低误点风险（skill destructive-nav-separation）。
 * - 每项 EP 官方图标（@element-plus/icons-vue，按需引入）+ 文字双标，提升识别度
 *   （skill nav-label-icon：禁 icon-only 导航，损害发现性）。
 *
 * Why 抽空 App.vue（上一轮工业级蜕变曾把终端 Grid 直接放在 App.vue）：
 * - 引入多路由后需 vue-router 多页结构，App.vue 退化为纯路由壳，
 *   保持「根组件只承载导航与路由出口」的 Vue 标准骨架。
 */
import { useRoute } from 'vue-router'
import { computed, type Component } from 'vue'
// 导航图标：EP 官方图标包，按需引入（非重型依赖，EP 生态标准配套）
import { TrendCharts, MagicStick, DataBoard, Files, Monitor, DataAnalysis, View } from '@element-plus/icons-vue'

const route = useRoute()
const activeName = computed(() => route.path)

// 导航项类型：路由 + 文字 + 图标组件
interface NavItem {
  to: string
  label: string
  icon: Component
}

// 左段：研究/配置（蔡森形态学流水线 Phase 3 起：蔡森筛选作为研究第一入口，
// 放 researchNav 首位；Spec 2 起参数实验室紧随其后——选股 → 调参的研究动线；
// 宏观驾驶舱/数据湖/AI 复盘依次承接）
const researchNav: NavItem[] = [
  { to: '/caisen',     label: '蔡森筛选',   icon: TrendCharts },
  { to: '/lab',        label: '参数实验室', icon: DataAnalysis },
  { to: '/dashboard',  label: '宏观驾驶舱', icon: DataBoard },
  { to: '/data',       label: '数据湖',     icon: Files },
  { to: '/review',     label: 'AI 复盘',    icon: MagicStick },
]

// 右段：实盘（唯一真实下单的高危入口，分隔线区隔）。
// 含「综合看板」(/cockpit)：观测俯瞰视角聚合心跳/资金/数据健康/流水/日志/回测对比，
// 与「实盘中控」(/live，含真下单/撤单) 同段但只读。Task 12 新增入口。
const liveNav: NavItem[] = [
  { to: '/cockpit', label: '综合看板', icon: View },
  { to: '/live', label: '实盘中控', icon: Monitor },
]
</script>

<template>
  <div class="app-shell">
    <!-- 顶部导航：暗黑细条，brand + 研究/配置段 ｜ 实盘段 -->
    <nav class="top-nav">
      <span class="nav-brand">Quanter</span>

      <!-- 研究/配置段 -->
      <router-link
        v-for="item in researchNav"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        :class="{ active: activeName === item.to }"
      >
        <el-icon :size="14"><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </router-link>

      <!-- 分隔线：物理区隔实盘高危入口 -->
      <span class="nav-divider" aria-hidden="true" />

      <!-- 实盘段 -->
      <router-link
        v-for="item in liveNav"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        :class="{ active: activeName === item.to }"
      >
        <el-icon :size="14"><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </router-link>
    </nav>

    <!-- 路由出口：各 View 在此渲染 -->
    <router-view />
  </div>
</template>

<style scoped>
/* 根壳：极夜黑底色，纵向 flex（导航 + 路由出口） */
.app-shell {
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--qt-bg-page);
}

/* 顶部导航：固定高度，卡片底色 + 极弱灰下边框分隔主体 */
.top-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 36px;
  padding: 0 var(--qt-space-3);
  background: var(--qt-bg-card);
  border-bottom: 1px solid var(--qt-border);
  flex-shrink: 0;
}

.nav-brand {
  font-size: 13px;
  font-weight: 700;
  color: var(--qt-accent); /* Quant 蓝，与全局 primary 同源 */
  letter-spacing: 0.5px;
  margin-right: var(--qt-space-2);
}

/*
 * 导航项：图标 + 文字双标（inline-flex 对齐），默认次要灰，hover 抬升底色，
 * 激活态高亮 Quant 蓝（低透蓝底锚定当前页）。
 * 焦点环由全局 :focus-visible 覆盖（terminal.css），键盘 Tab 可见。
 */
.nav-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--qt-text-secondary);
  text-decoration: none;
  padding: 4px var(--qt-space-2);
  border-radius: var(--qt-radius-sm);
  white-space: nowrap;
  transition: color 0.15s, background-color 0.15s;
}

.nav-item:hover {
  color: var(--qt-text-regular);
  background: var(--qt-bg-elevated);
}

.nav-item.active {
  color: var(--qt-accent);
  /* rgba(41,98,255,0.12) = --qt-accent (#2962ff) @ 12% 透明，锚定当前页 */
  background: rgba(41, 98, 255, 0.12);
}

/* 分隔线：区隔实盘高危入口（destructive-nav-separation） */
.nav-divider {
  width: 1px;
  height: 18px;
  background: var(--qt-border);
  margin: 0 var(--qt-space-2);
}
</style>
