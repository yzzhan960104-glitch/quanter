/**
 * Vue Router 配置（T17 路由恢复 + 宏观驾驶舱接入）
 *
 * 路由规则：
 * - /           → TerminalView（回测终端：ParamForm/ProChart/TerminalLogs 等）
 * - /dashboard  → DashboardView（宏观·板块驾驶舱：CreditRegime/三因子/板块/活跃池）
 *
 * Why 直接 import 而非懒加载：
 * - 项目仅 2 个页面，首屏加载即可，懒加载反而引入额外 chunk 请求增加首屏延迟；
 * - TerminalView 是默认入口页，必须同步加载；DashboardView 体量也小（4 个
 *   ECharts 面板），合并进主 chunk 在 gzip 后体积可控。
 *
 * Why 移除旧的 SingleBacktest / PortfolioBacktest 路由：
 * - 上一轮工业级蜕变已把回测终端编排上提到 App.vue（现 TerminalView），
 *   SingleBacktest.vue 是蜕变前的旧视图（与终端共享状态不兼容），保留会
 *   产生死代码 + 类型检查负担；PortfolioBacktest 同理待组合模式重启时
 *   再以 TerminalView 子模式接入，不在路由层挂裸页。
 */
import { createRouter, createWebHistory } from 'vue-router'
import TerminalView from '../views/TerminalView.vue'
import DashboardView from '../views/DashboardView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'terminal',
      component: TerminalView,
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: DashboardView,
    },
  ],
})

export default router
