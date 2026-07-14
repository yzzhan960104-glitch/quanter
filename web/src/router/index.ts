/**
 * Vue Router 配置（蔡森形态学流水线 Phase 3 Task 8：首页改指 /caisen）
 *
 * 当前路由（6 条）：
 * - /           → 重定向 /caisen（蔡森筛选作为研究/配置首屏，形态学流水线入口）
 * - /caisen     → CaisenScreenView（蔡森形态学筛选：Tick 缓存 → MA/无敌量/KZ → 结果展示）
 * - /lab        → ParamLabView（参数实验室：异步回测 master-detail + 轮询 + 参数调优，Spec 2）
 * - /dashboard  → DashboardView（宏观·板块驾驶舱）
 * - /live       → LiveCockpitView（实盘交易中控：EMT/QMT 连接 + 下单 + 订单/资产）
 * - /data       → DataLakeView（数据湖资产白盒反射）
 * - /review     → ReviewView（AI 复盘：GLM + 实盘日志 → Markdown 诊断报告）
 *
 * Why 全部懒加载（含 CaisenScreenView/DashboardView）：
 * - 首页 /caisen 形态学筛选面板体量中等，懒加载后主 chunk 更聚焦；
 * - 各 View 互不依赖，按路由切片可显著降低首屏主 bundle 体积。
 */
import { createRouter, createWebHistory } from 'vue-router'
import LiveCockpitView from '../views/LiveCockpitView.vue'
const CaisenScreenView = () => import('../views/CaisenScreenView.vue')
const ParamLabView = () => import('../views/ParamLabView.vue')
const DashboardView = () => import('../views/DashboardView.vue')
const DataLakeView = () => import('../views/DataLakeView.vue')
const ReviewView = () => import('../views/ReviewView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 首页改指蔡森筛选：形态学流水线为研究/配置第一入口（Phase 3 起）
    { path: '/', redirect: '/caisen' },
    { path: '/caisen', name: 'caisen', component: CaisenScreenView },
    // 参数实验室（Spec 2：异步回测 master-detail + 轮询；紧跟蔡森筛选，研究动线：选股 → 调参）
    { path: '/lab', name: 'lab', component: ParamLabView },
    { path: '/dashboard', name: 'dashboard', component: DashboardView },
    { path: '/live', name: 'live', component: LiveCockpitView },
    { path: '/data', name: 'data', component: DataLakeView },
    { path: '/review', name: 'review', component: ReviewView },
  ],
})

export default router
