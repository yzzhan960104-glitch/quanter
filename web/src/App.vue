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
    </nav>

    <!-- 路由出口：TerminalView / DashboardView 在此渲染 -->
    <router-view />
  </div>
</template>

<style scoped>
/* 根壳：暗黑底色，纵向 flex（导航 + 路由出口） */
.app-shell {
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: #0d1117;
}

/* 顶部导航：固定高度，暗卡片底色，细分隔线分隔主体 */
.top-nav {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 36px;
  padding: 0 16px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}

.nav-brand {
  font-size: 13px;
  font-weight: 700;
  color: #58a6ff;
  letter-spacing: 0.5px;
}

/* 导航项：默认灰，激活态高亮蓝（下划线锚定当前页） */
.nav-item {
  font-size: 12px;
  color: #8b949e;
  text-decoration: none;
  padding: 4px 8px;
  border-radius: 4px;
  transition: color 0.15s;
}

.nav-item:hover {
  color: #c9d1d9;
}

.nav-item.active {
  color: #58a6ff;
  background: rgba(88, 166, 255, 0.1);
}
</style>
