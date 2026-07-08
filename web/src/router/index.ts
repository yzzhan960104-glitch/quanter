/**
 * Vue Router 配置（蔡森专精化 Phase 1：删除回测/因子/策略前端）
 *
 * 当前路由（4 条）：
 * - /           → 临时重定向 /dashboard（回测终端已删；Phase 3 建 CaisenScreenView 后改指 /caisen）
 * - /dashboard  → DashboardView（宏观·板块驾驶舱）
 * - /live       → LiveCockpitView（实盘交易中控：EMT/QMT 连接 + 下单 + 订单/资产）
 * - /data       → DataLakeView（数据湖资产白盒反射）
 * - /review     → ReviewView（AI 复盘：GLM + 实盘日志 → Markdown 诊断报告）
 *
 * Why DashboardView 直接 import、其余懒加载：
 * - DashboardView 是临时首页（首屏必加载），体量小（ECharts 面板 gzip 后可控）；
 * - DataLake/Review 体量较大且非首屏，按路由懒加载降低首屏主 chunk 体积。
 */
import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import LiveCockpitView from '../views/LiveCockpitView.vue'
const DataLakeView = () => import('../views/DataLakeView.vue')
const ReviewView = () => import('../views/ReviewView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 首页临时指宏观驾驶舱（回测终端已删；Phase 3 建 CaisenScreenView 后改指 /caisen）
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', name: 'dashboard', component: DashboardView },
    { path: '/live', name: 'live', component: LiveCockpitView },
    { path: '/data', name: 'data', component: DataLakeView },
    { path: '/review', name: 'review', component: ReviewView },
  ],
})

export default router
