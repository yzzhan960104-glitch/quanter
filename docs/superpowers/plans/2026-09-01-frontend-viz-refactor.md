# 前端可视化重构方案（2026-09-01）

> 交付对象：yzzhan.xin 公网站 + 内网 LAN 站（同一代码库双态）
> 红线：后端纯只读架构不动（写端点已全量退役）；双态模式不动（静态公开/在线内网）；
> gate② 契约链保持绿。本方案只动"看"，不动"跑"。

## 一、诊断：为什么"太差"

| # | 事实 | 后果 |
|---|---|---|
| 1 | 12 个业务组件里只有 2 个图表（都在 Discovery 视图）；cockpit/experiments 全是 el-table 文字墙 | 量化系统最该有的图一张都没有 |
| 2 | **lightweight-charts 5.2（TradingView K 线库）已装、零使用**；echarts 5.5 + vue-echarts 7 + terminal-dark 主题已有，只在 1 个视图用 | 弹药在库房，前线用刺刀 |
| 3 | 无净值曲线、无回撤图、无 K 线形态、无盈亏分布、无日历热图 | "钱赚不赚、信号准不准"两大第一性问题不可视 |
| 4 | Dashboard 视图空壳化（宏观 CTA/CreditRegime/板块图已退役，剩占位） | 死视图占导航位 |
| 5 | 双腿 A/B 有结构化对照数据（signals/orders diff、gate counts）但只渲染文字 | 对照价值没被释放 |
| 6 | 全站桌面优先（el-row 固定 span，无响应式断点） | 公网站手机体验差——公网主要在手机看 |
| 7 | 公网首屏直接是 cockpit 表格，没有"这是什么系统"的叙事 | 访客 3 秒看不懂 |

## 二、目标：量化驾驶舱的四个第一性问题

重构后的信息架构围绕四问组织，每问一个图表族：

1. **钱赚不赚** → 净值曲线族（P1）
2. **信号准不准** → K 线形态回放族（P2）
3. **仓稳不稳** → 持仓风险面族（P3）
4. **系统健不健康** → 漏斗/对照/数据健康（P3-P4，部分已有）

## 三、图表族设计（具体到库/数据源/交互/落点）

### 族 1：净值曲线（ECharts，P1）

| 图 | 形态 | 数据源 | 交互 |
|---|---|---|---|
| 净值主线 | 双腿双线 + 可选沪深300 基准线（index_daily 已在湖） | `nav_history.json` 新快照 | 十字光标、区间缩放、腿开关 |
| 回撤水下图 | area 负值填充 | 同上派生 | 与主图 dataZoom 联动 |
| 日收益柱 | 正负双色柱状 | 同上派生 | 悬停显日 PnL/％ |
| 日历热图 | GitHub 贡献图风格月历（日 PnL 着色） | 同上派生 | 月切换 |

**数据管道（关键新增）**：
- `public_snapshot.py` 增 nav 历史 append：每次发布把当日 nav 追加进 `web/public/data/nav_history.json`（幂等：同日覆盖）
- **回填源现成**：`logs/emquant_eod_{main,exp}_YYYY-MM-DD.txt` 从 08-28 起每行都有 nav——写一次性回填脚本，GM 双腿净值史当天即可拉通（20 万入金起点为基准 1.0 归一）
- 旧引擎 `account_daily`（QMT 时代，止于 08-27）不混入：时代不同账户不同，混画=误导。如需展示，单独"史前时代"开关

### 族 2：K 线形态回放（lightweight-charts 主力，P2）

策略是颈线法——**把信号画回 K 线上是本系统最该有的图**：

| 元素 | 呈现 |
|---|---|
| 日 K 主图 | candlestick（lightweight-charts） |
| 颈线/底部 | 水平 priceLine + 标签 |
| entry/止损/TP1/TP2 | 彩色 priceLine + 右侧标签（与 EOD 播报同色语义） |
| 信号日 | K 线 marker（▲）+ formed_at 标注 |
| 成交/离场 | marker（●买/●卖，从 audit fill/expiry） |

数据源双态：
- **静态（公网）**：快照模式预生成持仓标的 + 当日信号标的的 `ohlcv_<sym>.json`（每标的 160 根日 K ≈ 几 KB，≤15 标的，体积无忧）；marklines 数据并入（neckline/stop/tp 从 state.pkl 与 audit SIGNAL detail）
- **在线（内网）**：新增唯一只读端点 `GET /api/v1/market/ohlcv?symbol=&bars=`（读 a_shares_daily.parquet 纯读，走 gate② 契约注册 facade）——任意标的懒加载
- 入口动线：持仓卡/audit SIGNAL 行/晨检计划段 → 点击标的 → K 线抽屉（drawer）展开

### 族 3：持仓风险面（ECharts，P3）

| 图 | 形态 | 语义 |
|---|---|---|
| 盈亏横条图 | 按腿分组、正负双色、按浮盈排序 | 一眼看出谁在养组合谁在拖后腿 |
| 持仓散点气泡 | x=持仓天数（含上限30线）y=浮盈% 气泡=市值 | 超期风险与盈利质量同屏 |
| 仓位集中度 | 环形图（单票市值占比 + 7.5% 上限线） | 集中度红线可视化 |
| 今日流水时间轴 | 成交点时间轴（价+量+方向） | 当日作战回放 |

数据全部来自现有快照（positions/audit/fills），零管道新增。

### 族 4：信号漏斗与双腿对照（ECharts，P3-P4）

- **漏斗图**：信号 N→挂单 M→成交 K→拦截分桶（audit counts 已有，AbGate.counts）
- **A/B 对照分面**：双腿 gate counts 并排柱 + signals/orders diff 高亮表（数据在 ab JSON 里躺着）
- **数据健康**：DataHealthCard 升级为 43 数据集状态时间线（datasets 历史可 append 进 nav_history 同款管道）

## 四、视图层重组

### 公网站（静态态）
```
/            首页=净值总览（英雄区：净值曲线+回撤+今日摘要三卡+只读声明）
/positions   持仓（K线抽屉+风险面）
/experiments 实验对照（漏斗+对照分面+事件流）
/data        数据湖健康
```
### 内网站（在线态）
- 保留 /discovery（唯一已达标视图，P4 顺手把 terminal-dark 主题细节对齐）
- /dashboard 死视图**退役**（不填充——宏观面没有回归计划，占位即负债）
- cockpit 与公网首页合并心智：内网多 TerminalLogs SSE 卡

### 移动端（P3，与图表族同步）
- el-table → 断点卡片化（<768px 表格降级为卡列表，EP 支持 cell 渲染改造）
- 图表容器 100% 宽 + 高度钳制；K 线抽屉在移动端变全屏
- 首页英雄区移动端单列堆叠

## 五、技术决策

| 决策 | 选择 | 理由 |
|---|---|---|
| K 线库 | **lightweight-charts**（已装未用） | 十字光标/缩放/性能原生；echarts candlestick 交互弱 |
| 其余图表 | ECharts + vue-echarts（已有）+ terminal-dark 主题铺满全站 | 主题资产复用，视觉统一 |
| 图表组件化 | `src/components/charts/` 四族通用组件，数据经 facade 注入 | 双态同构：静态 fetch / 在线 apiClient，与现有 gm.ts 模式一致 |
| UI 框架 | Element Plus 留任 | 不为换而换；表格→卡片用现有 EP 组件可完成 |
| 打包 | 图表库按视图动态 import（路由级分包已有） | 现 index 1.31MB 主包，lightweight-charts 按需进 K 线视图 chunk |
| SSR/SEO | 不做 | 单页+微信分享 meta 够用，不值复杂度 |

## 六、分期落地（每期独立可发布、独立可见）

| 期 | 内容 | 数据管道 | 量级 |
|---|---|---|---|
| **P1** | 净值曲线+回撤+日历热图+公网首页重组+nav 回填 | nav_history append + eod 日志回填 | ~1 天 |
| **P2** | K 线形态回放（持仓+当日信号标的，静态预生成 + 内网 ohlcv 端点） | ohlcv 快照 + 1 个只读端点 | ~1 天 |
| **P3** | 持仓风险面四图+漏斗+移动端断点适配 | 零新增（现有快照） | ~1 天 |
| **P4** | A/B 对照分面+Discovery 主题对齐+/dashboard 退役清理 | 零新增 | ~0.5 天 |

顺序理由：P1 价值最大且当天见效（回填即有 5 天史）；P2 是差异化核心（量化系统没有 K 线说不过去）；P3 兜底体验；P4 收尾。

## 七、验证与护栏

- 每期走 `ops/run_checks.py` 五链（gate② 新端点需配套 facade + 契约绿）
- 公网构建走 `build:public`：脱敏扫描（account/token 关键词 grep 零命中）+ 体积检查（图表 chunk 增量 <200KB gzip）
- 每期发布后人工过一遍 yzzhan.xin 手机端

## 八、明确不做（纪律清单）

- 不引入任何写交互（公网站物理只读是架构资产，不是待修缺陷）
- 不加实时推送（公网静态定死；内网 SSE 已有）
- 不换框架/UI 库；不做 SSR；不做多语言
- 不用旧引擎 account_daily 混画净值（时代口径分离）
- 不给 /dashboard 填充新内容（退役而非复活）
