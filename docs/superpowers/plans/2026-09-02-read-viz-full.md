# 全读功能可视化方案（P5-P7 · 2026-09-02）

> 背景：用户裁决站点终态=私域（鉴权建设中，功能先行）。目标=服务端 21 个只读端点
> + 全部数据资产 100% 可视化。本方案承接 docs/superpowers/plans/2026-09-01-frontend-viz-refactor.md
> （P1-P4 已完成净值族/K线回放/持仓/首页重组/亮色主题）。

## 〇、架构不变量（所有新功能必须遵守）

1. **双态模式**：数据一律进 `ops/public_snapshot.py` 快照管道（`web/public/data/*.json`），
   前端经 `src/api/home.ts` facade `staticGet` 消费；在线态复用同文件（LAN dist 随包）。
   新增服务端端点=最后手段（本批仅 trailing 无需、processes 用快照时点捕获）。
2. **发布链不动**：`ops/publish_public.py`（快照→build:public→wrangler 直推）；交易日
   9:35-15:35 每时 + 18:10 预演 cron（server 重启后生效）。
3. **脱敏底线保留**：键名含 token/secret 剔除、account* 掩码前 8 位——私域终态也
   不放松（零成本防线）。
4. **视觉规范**：亮色主题（terminal-light）、`--qt-*` token、图表组件放
   `src/components/charts/`、卡片复用 el-card shadow=never、ECharts 滚轮语义=
   纯滚轮翻页/Ctrl+滚轮缩放。
5. **验证**：每功能过五链（run_checks）+ build:public 产物检查 + 发布后线上 JSON 实测；
   `run_checks | tail` 管道吞退出码的坑已犯过——必须 `echo EXIT=$?`。
6. **新视图三件套**：根容器 `flex:1; overflow-y:auto`（滚动锁事故）、锚点导航（多卡页）。
7. **私域窗口期**：预演等敏感内容按用户裁决已公开；窗口期内理论前跑面由用户自担，
   CF Access 一句话可挂（见 P7）。

## 一、P5（第一批：高频刚需 + 风险透明，~1.5 天）

### 5.1 当日委托全景 OrdersPanel ★★★
- **数据**：`gm_orders_{leg}.json` 已在快照（7002 orders 全状态）——零管道新增。
- **组件**：`src/components/cockpit/OrdersPanel.vue`；挂 CockpitView 流水卡旁
  （或 Tab 切换"成交|委托"——推荐 Tab 省空间）。
- **列**：时间/标的+名/方向/价格/数量/已成交/状态（el-tag：全部成交=成交/部分/
  已拒=红+拒因 tooltip/已撤）/委托号。status 映射：3=成交 8=拒（ordRejReason）
  5/6=撤。
- **验收**：拒单行显示拒因（科创板 200 股历史案例可在 09-01 快照回验——需快照
  保留当日 orders；当日才有，历史无碍）。

### 5.2 持仓行业分布 + 资产构成 ★★★
- **数据**：行业=stock_basic.parquet `industry` 列（快照新增 positions_enrich 行业
  聚合进 `gm_positions_{leg}.json` 或独立 `positions_meta.json`）；资产构成=
  nav_history 已有 nav，缺 cash/mv 历史→**扩展 nav_history append**（每日
  available/market_value 一并落）。
- **组件**：`AllocationCard.vue`（首页或 cockpit）：行业环形图（ECharts pie，
  按市值）+ 现金/市值堆叠面积图（nav_history 扩展字段）。
- **验收**：环形图行业数=持仓行业去重数；堆叠图现金+市值=nav 逐日闭合。

### 5.3 Trailing 止损轨迹 ★★★
- **数据**：state.pkl 每持仓 trailing 六件套（neckline/atr/stop_atr_mult/grace/
  step/floor）+ 湖日线。**轨迹重构**：importlib 加载部署产物调 `compute_stop_price`
   逐日回放（holding_days=0..N，价=当日 close 近似）→ 快照在 ohlcv marks 里
  增 `trailing_path: [[date, stop], ...]`。
- **组件**：KlinePanel 增画 trailing 阶梯线（绿色虚线阶梯，区别于静态止损实线）。
- **验收**：末点=当前止损位（与持仓表 stop 列一致）；阶梯只在 grace 后抬升。

### 5.4 运维健康面板 OpsHealthView `/ops` ★★☆
- **数据**：a) `logs/trading_job_run.db` job_run 表（近 14 日：pipeline/晨检/EOD/
  播报/预演 状态+耗时）；b) `logs/alerts.log` 尾部 200 行解析为告警时间线；
  c) 进程拓扑快照时点捕获（wmic/psutil 列 uvicorn/策略进程/bot 存在性）。
  快照新增 `ops_health.json`。
- **组件**：`OpsHealthView.vue`：任务台账表（日期×任务 状态格）+ 告警时间线
  （el-timeline，ERROR 红/WARN 橙）+ 进程卡（六进程存活灯）。
- **导航**：公开态+在线态都挂（"作业驾驶舱"烂尾名分兑现）。
- **验收**：台账与 db 行数一致；告警行数=解析行数。

## 二、P6（第二批：研究线与纵深，~1.5 天）

### 6.1 研究提案流 ProposalsView `/research`
- **数据**：`logs/research_proposals.db` 直读（status/hypothesis/params/时间戳）
  → 快照 `proposals.json`（含状态管道统计）。
- **组件**：状态管道看板（created→verified→published 三列卡片或时间线表）+
  参数 diff 高亮（相邻版本）。

### 6.2 版本演进史 VersionHistory
- **数据**：experiment/store（versions 列表+best_annual+created_at）→ 快照
  `experiment_versions.json`。
- **组件**：腿详情页新增"版本演进"卡：时间线+outer 年化条形对比（R6-8 vs B3
  等全谱）。

### 6.3 周度回测队列 + digest 摘要
- **数据**：replay_tasks.db（PENDING/RUNNING/DONE 状态）+ logs/research_digest
  产物（回测预期 vs 实盘）→ `backtest_queue.json`。
- **组件**：运维面板内两卡（或独立）：队列状态表 + 预期/实际对照条。

### 6.4 机会观察扩池
- 管道参数化现状：`ops/tsb_data.py` fetchers dict——铜（新浪 CU0/湖 江西铜业?）、
  原油（新浪 CL）、白银（SI）+ 对应 A 股（江西铜业/中国石油/盛达资源等）。
  每组=fetcher 两行+视图一张卡（复制现有 mkOption 模式）。

## 三、P7（鉴权与内网面开放，~1 天）

1. **鉴权**：CF Access（Zero Trust→Applications→self-hosted `yzzhan.xin`→
   Email OTP policy 允许 615213345@qq.com）——零代码，30 分钟；终态可换自建
   （FastAPI session / JWT），暂不设计细节。
2. 鉴权后公网开放：SSE 日志流（TerminalLogs 撤 staticMode 隐藏）、Discovery
   研究面（/discovery 路由+导航解禁）、活跃股池 /macro/pool。
3. **processes 端点前端化**（若 5.4 快照时点捕获不满足实时需求）：LAN 态直连
   GET /processes。

## 四、里程碑与提交纪律

- 每功能独立 commit（feat(web): …），过五链后提交；批末发布+线上实测。
- 顺序：5.1→5.2→5.3→5.4（P5 一天半）→ 6.x → P7。
- 预计总量 ~4 天 agent 工时，分 2-3 个 session。

## 五、明确不做

- 不加写交互；不动策略/后端只读架构；不为可视化造新 DB（全读现有资产）；
  Dashboard 空壳视图不复活（宏观面走 P7 的 /macro/pool 独立卡）。
