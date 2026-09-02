/**
 * Vue Router 配置（caisen 形态学退役后 · 颈线法 + discovery 主线）。
 *
 * 当前路由（6 条，2026-08-13 · G8 caisen 死视图清理后）：
 * - /           → 重定向 /discovery（搜索实验室：参数发现敏感性分析/热力图，研究第一入口）
 * - /discovery  → DiscoveryLabView（搜索实验室：敏感性仪表板/热力图/搜索进展，spec §4.3 只读）
 * - /dashboard  → DashboardView（活跃股池驾驶舱：2026-08-15 CR-8 删板块图块后单块布局）
 * - /live       → 已退役，重定向 /cockpit（2026-08-27 · QMT 退役 P1）
 * - /data       → DataLakeView（数据湖资产白盒反射）
 * - /cockpit    → CockpitView（综合看板：聚合流水/日志/心跳/资金/数据健康）
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
import { STATIC_MODE } from '../api/static'
const DashboardView = () => import('../views/DashboardView.vue')
const DataLakeView = () => import('../views/DataLakeView.vue')
// 综合看板（Task 12 · 一期观测运营层前端收官）：聚合流水/日志/心跳/资金/数据健康。
const CockpitView = () => import('../views/CockpitView.vue')
const ExperimentView = () => import('../views/ExperimentView.vue')
// 作业驾驶舱（Phase 2 · Task 12 收官）：当天 pipeline/pre_open 台账 + 启动补跑四态（只读）。
// 搜索实验室（P3 · 2026-08-13）：参数发现敏感性分析/热力图/进展（只读，spec §4）。
const DiscoveryLabView = () => import('../views/DiscoveryLabView.vue')
// 公网首页（可视化重构 P1 · 2026-09-01）：净值曲线族英雄页——访客 3 秒看懂"赚不赚"。
const HomeView = () => import('../views/HomeView.vue')
// 腿详情（2026-09-01 需求②）：策略全量信息+风控参数（静态档案消费）。
const LegDetailView = () => import('../views/LegDetailView.vue')
// 机会观察（2026-09-02）：紫金×纽约金、美元×US10Y×纽约金、海胶×沪胶。
const TsbView = () => import('../views/TsbView.vue')
// 运维健康面板（P5.4 · 2026-09-03）：任务台账+告警时间线+进程拓扑（快照时点）。
const OpsHealthView = () => import('../views/OpsHealthView.vue')

// 公网静态模式（VITE_STATIC_DATA=1 · yzzhan.xin）：路由收敛为公开观测面——
// 首页(净值总览)/cockpit/experiments/data/ops（数据健康度）。discovery（研究 IP）
// 与 dashboard（内网宏观）不公开；未知路径兜底回首页（公开站不暴露内部 404 面）。
const staticRoutes = [
  { path: '/', name: 'home', component: HomeView },
  { path: '/cockpit', name: 'cockpit', component: CockpitView },
  { path: '/leg/:leg', name: 'leg-detail', component: LegDetailView },
  { path: '/opportunity', name: 'opportunity', component: TsbView },
  { path: '/experiments', name: 'experiments', component: ExperimentView },
  { path: '/data', name: 'data', component: DataLakeView },
  { path: '/ops', name: 'ops', component: OpsHealthView },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter(
  STATIC_MODE
    ? { history: createWebHistory(), routes: staticRoutes }
    : {
        history: createWebHistory(),
        routes: [
    // 首页改指搜索实验室：caisen 退役后，参数发现敏感性分析作为研究第一入口（spec §4 P3）。
    { path: '/', redirect: '/discovery' },
    // 搜索实验室（P3 · spec §4.3）：敏感性仪表板 + 热力图 + 搜索进展（只读，研究动线首屏）。
    { path: '/discovery', name: 'discovery', component: DiscoveryLabView },
    { path: '/dashboard', name: 'dashboard', component: DashboardView },
    // '/live 实盘中控' 已退役（2026-08-27 · QMT 退役 P1）：重定向综合看板
    { path: '/live', redirect: '/cockpit' },
    // 综合看板（Task 12）：实盘观测俯瞰入口，聚合心跳/资金/数据健康/流水/日志。
    { path: '/cockpit', name: 'cockpit', component: CockpitView },
  { path: '/leg/:leg', name: 'leg-detail', component: LegDetailView },
  { path: '/opportunity', name: 'opportunity', component: TsbView },
    // 实验对照（2026-08-29 多腿方案 P3）：双腿 A/B 的轮次/对照/下钻视图
    { path: '/experiments', name: 'experiments', component: ExperimentView },
    // 数据湖（Task 12 原作业驾驶舱语义收编进 /ops · P5.4）。
    { path: '/data', name: 'data', component: DataLakeView },
    // 运维健康面板（P5.4）：任务台账+告警+进程拓扑（作业驾驶舱 /jobs 名分兑现，
    // 在线态与公开态同组件——数据全走 ops_health.json 快照）。
    { path: '/ops', name: 'ops', component: OpsHealthView },
    { path: '/opportunity', name: 'opportunity', component: TsbView },
        ],
      },
)

export default router
