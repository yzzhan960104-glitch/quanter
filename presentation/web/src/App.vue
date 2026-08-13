<script setup lang="ts">
/**
 * 应用根壳（路由导航 + 出口）
 *
 * 职责：
 *   1. 顶部导航条：在 4 个功能页间切换（图标 + 文字双标，按使用动线分组）
 *   2. <router-view/> 渲染当前路由对应的视图
 *
 * 导航信息架构（caisen 形态退役后 · G8 清理；Phase 1 · 前端只读化 Task 6 撤 AI 复盘）：
 * - 左段「研究/配置」3 项：搜索实验室（参数发现敏感性分析，研究第一入口）→
 *   宏观驾驶舱 → 数据湖（按研究动线：参数发现 → 宏观面 → 数据资产）。
 *   caisen 退役（G8）：原「蔡森筛选」+「参数实验室」随 server/api/v1/caisen.py 整删
 *   而移除（前端 caisen.ts 调的 6 个端点全 404，CaisenScreenView/ParamLabView 死视图）；
 *   首页由 /caisen 改指 /discovery，研究动线第一入口由 discovery 参数发现敏感性承接。
 * - 右段「实盘」1 项：实盘中控。用 .nav-divider 细分隔线物理区隔——这是全站唯一会
 *   真实下单的高危入口，空间区隔降低误点风险（skill destructive-nav-separation）。
 * - 顶栏常驻「READ-ONLY 只读」红色徽标（Phase 1 · 前端只读化）：所有写操作走
 *   脚本/CLI/QMT 客户端/cron，徽标在视觉上把这一约束广播给每位使用者，杜绝误操作。
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
// Phase 1 · 前端只读化 Task 6：撤 MagicStick（AI 复盘导航项随 ReviewView 整删）。
// G8 caisen 死视图清理：撤 TrendCharts（蔡森筛选）/ DataAnalysis（参数实验室）——
//   对应导航项随 CaisenScreenView/ParamLabView 整删而移除，图标无消费者亦撤。
import { DataBoard, Files, Monitor, View, Operation } from '@element-plus/icons-vue'

const route = useRoute()
const activeName = computed(() => route.path)

// 导航项类型：路由 + 文字 + 图标组件
interface NavItem {
  to: string
  label: string
  icon: Component
}

// 左段：研究/配置（caisen 形态退役后 · G8 清理：搜索实验室作为研究第一入口，
// 放 researchNav 首位；宏观驾驶舱/数据湖依次承接）。
// Phase 1 · 前端只读化 Task 6：撤「AI 复盘」项（diagnose 为写操作，随 ReviewView 整删）。
// G8（2026-08-13）：撤「蔡森筛选」+「参数实验室」（caisen.ts 调死端点，CaisenScreenView
//   + ParamLabView 整删让 check_contracts gate② 绿；首页改指 /discovery）。
const researchNav: NavItem[] = [
  // 搜索实验室（P3）：参数发现敏感性分析/热力图（只读，spec §4，研究动线首屏）
  { to: '/discovery',  label: '搜索实验室', icon: DataBoard },
  { to: '/dashboard',  label: '宏观驾驶舱', icon: DataBoard },
  { to: '/data',       label: '数据湖',     icon: Files },
]

// 右段：实盘（唯一真实下单的高危入口，分隔线区隔）。
// 含「综合看板」(/cockpit)：观测俯瞰视角聚合心跳/资金/数据健康/流水/日志/回测对比，
// 与「实盘中控」(/live，含真下单/撤单) 同段但只读。
// Phase 2 · Task 12 新增「作业驾驶舱」(/jobs)：当天 pipeline/pre_open 台账 + 启动补跑四态，
// 置于「综合看板」与「实盘中控」之间（按"全局俯瞰 → 调度台账 → 真实下单"的观测深入动线）。
const liveNav: NavItem[] = [
  { to: '/cockpit', label: '综合看板', icon: View },
  { to: '/jobs', label: '作业驾驶舱', icon: Operation },
  { to: '/live', label: '实盘中控', icon: Monitor },
]
</script>

<template>
  <div class="app-shell">
    <!-- 顶部导航：暗黑细条，brand + 研究/配置段 ｜ 实盘段 -->
    <nav class="top-nav">
      <span class="nav-brand">Quanter</span>
      <!-- READ-ONLY 只读徽标：Phase 1 前端只读化，所有写操作走脚本/CLI/QMT 客户端/cron -->
      <span class="ro-badge" title="前端只读：所有写操作走脚本/CLI/QMT 客户端/cron">READ-ONLY 只读</span>

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

/* READ-ONLY 只读徽标（Phase 1 · 前端只读化）：红色底 + 白字小号胶囊，常驻顶栏，
   把"前端不发起任何写操作"的约束显式广播给使用者，杜绝误操作（copy/CSS 来自 spec） */
.ro-badge {
  font-size: 10px; font-weight: 700; color: #fff;
  background: #c62828; padding: 2px 6px; border-radius: 3px;
  margin-left: var(--qt-space-2); letter-spacing: 0.3px;
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
