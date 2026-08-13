/**
 * Vue Router 配置（caisen 形态学退役后 · 颈线法 + discovery 主线）。
 *
 * 当前路由（6 条，2026-08-13 · G8 caisen 死视图清理后）：
 * - /           → 重定向 /discovery（搜索实验室：参数发现敏感性分析/热力图，研究第一入口）
 * - /discovery  → DiscoveryLabView（搜索实验室：敏感性仪表板/热力图/搜索进展，spec §4.3 只读）
 * - /dashboard  → DashboardView（宏观·板块驾驶舱）
 * - /live       → LiveCockpitView（实盘交易中控：QMT 连接 + 下单 + 订单/资产）
 * - /data       → DataLakeView（数据湖资产白盒反射）
 * - /cockpit    → CockpitView（综合看板：聚合流水/日志/心跳/资金/数据健康）
 * - /jobs       → JobCockpitView（作业驾驶舱：当天 pipeline/pre_open 台账 + 启动补跑四态）
 *
 * Why 删 /caisen + /lab（2026-08-13 · G8 契约清理）：
 * - 后端 server/api/v1/caisen.py 随 caisen 形态整体退役已删（master 策略 = neckline，
 *   caisen 是历史废弃形态），前端 caisen.ts 调的 6 个 /api/v1/caisen/* 端点全 404，
 *   check_contracts gate② 漂移阻断 CI。CaisenScreenView（调 listPlans/getChart）+
 *   ParamLabView（调 getConfigSchema/listReplayTasks/getReplayTask）均为纯死视图，
 *   整删让契约对齐。
 * - ParamLabView 的「训练 loop 写交互」定位（spec §4.3）属未来设计意图，当前实现
 *   未接 training_router（/api/v1/training）；重建需配套 training.ts facade + 独立
 *   任务，本清理只让 gate② 绿，不复活死 UI。
 * - 首页 redirect 从 /caisen 改指 /discovery：caisen 退役后研究第一入口由 discovery
 *   参数发现敏感性分析承接（spec §4 · P3 可分析性主线），与「研究→实盘」动线一致。
 *
 * Why 全部懒加载（含 DashboardView）：
 * - 各 View 互不依赖，按路由切片可显著降低首屏主 bundle 体积。
 */
import { createRouter, createWebHistory } from 'vue-router'
import LiveCockpitView from '../views/LiveCockpitView.vue'
const DashboardView = () => import('../views/DashboardView.vue')
const DataLakeView = () => import('../views/DataLakeView.vue')
// 综合看板（Task 12 · 一期观测运营层前端收官）：聚合流水/日志/回测对比/心跳/资金/数据健康。
const CockpitView = () => import('../views/CockpitView.vue')
// 作业驾驶舱（Phase 2 · Task 12 收官）：当天 pipeline/pre_open 台账 + 启动补跑四态（只读）。
const JobCockpitView = () => import('../views/JobCockpitView.vue')
// 搜索实验室（P3 · 2026-08-13）：参数发现敏感性分析/热力图/进展（只读，spec §4）。
const DiscoveryLabView = () => import('../views/DiscoveryLabView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 首页改指搜索实验室：caisen 退役后，参数发现敏感性分析作为研究第一入口（spec §4 P3）。
    { path: '/', redirect: '/discovery' },
    // 搜索实验室（P3 · spec §4.3）：敏感性仪表板 + 热力图 + 搜索进展（只读，研究动线首屏）。
    { path: '/discovery', name: 'discovery', component: DiscoveryLabView },
    { path: '/dashboard', name: 'dashboard', component: DashboardView },
    { path: '/live', name: 'live', component: LiveCockpitView },
    // 综合看板（Task 12）：实盘观测俯瞰入口，聚合心跳/资金/数据健康/流水/日志。
    { path: '/cockpit', name: 'cockpit', component: CockpitView },
    // 作业驾驶舱（Phase 2 · Task 12）：当天 pipeline/pre_open 台账 + 启动补跑四态（只读）。
    { path: '/jobs', name: 'jobs', component: JobCockpitView },
    { path: '/data', name: 'data', component: DataLakeView },
  ],
})

export default router
