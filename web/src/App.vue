<script setup lang="ts">
/**
 * 应用根壳（T17 路由恢复）
 *
 * 职责收敛为：
 *   1. 顶部导航条（在「回测终端」/「宏观驾驶舱」两页间切换）
 *   2. <router-view/> 渲染当前路由对应的视图
 *
 * Why 抽空 App.vue（上一轮工业级蜕变曾把终端 Grid 直接放在 App.vue）：
 * - T17 引入 /dashboard 宏观驾驶舱，需恢复 vue-router 双页结构；
 * - 把终端 Grid 下移到 views/TerminalView.vue，App.vue 退化为纯路由壳，
 *   保持「根组件只承载导航与路由出口」这一 Vue 项目的标准骨架。
 * - 终端状态共享经 useTerminalState 模块级单例，视图切换不丢回测状态。
 */
import { useRoute } from 'vue-router'
import { computed } from 'vue'

// 当前路由名（用于高亮激活的导航项）
const route = useRoute()
const activeName = computed(() => route.path)
</script>

<template>
  <div class="app-shell">
    <!-- 顶部导航：暗黑细条，两个 router-link 切换 / 与 /dashboard -->
    <nav class="top-nav">
      <span class="nav-brand">Quanter</span>
      <router-link to="/" class="nav-item" :class="{ active: activeName === '/' }">
        回测终端
      </router-link>
      <router-link to="/dashboard" class="nav-item" :class="{ active: activeName === '/dashboard' }">
        宏观驾驶舱
      </router-link>
      <router-link to="/explorer" class="nav-item" :class="{ active: activeName === '/explorer' }">
        因子沙盒
      </router-link>
      <router-link to="/live" class="nav-item" :class="{ active: activeName === '/live' }">
        实盘中控
      </router-link>
      <router-link to="/data" class="nav-item" :class="{ active: activeName === '/data' }">
        数据湖
      </router-link>
      <router-link to="/factors" class="nav-item" :class="{ active: activeName === '/factors' }">
        因子
      </router-link>
      <router-link to="/strategies" class="nav-item" :class="{ active: activeName === '/strategies' }">
        策略
      </router-link>
      <router-link to="/backtest" class="nav-item" :class="{ active: activeName === '/backtest' }">
        归因回测
      </router-link>
      <router-link to="/review" class="nav-item" :class="{ active: activeName === '/review' }">
        AI 复盘
      </router-link>
    </nav>

    <!-- 路由出口：TerminalView / DashboardView 在此渲染 -->
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
  background: #131722; /* 极夜黑，与 --el-bg-color-page 同源 */
}

/* 顶部导航：固定高度，卡片底色 + 极弱灰下边框分隔主体 */
.top-nav {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 36px;
  padding: 0 16px;
  background: #1e222d;
  border-bottom: 1px solid #2b3139;
  flex-shrink: 0;
}

.nav-brand {
  font-size: 13px;
  font-weight: 700;
  color: #2962ff; /* Quant 蓝，与全局 primary 同源 */
  letter-spacing: 0.5px;
}

/* 导航项：默认次要灰，激活态高亮 Quant 蓝（低透蓝底锚定当前页） */
.nav-item {
  font-size: 12px;
  color: #787b86;
  text-decoration: none;
  padding: 4px 8px;
  border-radius: 4px;
  transition: color 0.15s;
}

.nav-item:hover {
  color: #b2b5be;
}

.nav-item.active {
  color: #2962ff;
  background: rgba(41, 98, 255, 0.12);
}
</style>
