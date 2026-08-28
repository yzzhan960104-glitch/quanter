# Cockpit 多腿交互方案:多策略实验 + 多账户展示

> 日期:2026-08-29 | 状态:待裁决(方案定稿)
> 背景:双腿体系已于 08-28 落地实跑(实验腿 b24c6a2f/主腿 d9324346,晨检/guard/ingest/对照已双腿化),但 presentation 层的 gm 只读代理(W4-B 复活产物)仍是**单腿硬编码**——`api/v1/gm.py` 的 `_proxy` 每次从主腿 runtime.json 取 account,cockpit 五组件展示的是且只是主账户。本方案把「腿」作为一等概念贯穿前后端。

## 一、设计原则(全部继承既有纪律,零新发明)

| 纪律 | 落法 |
|---|---|
| 只读红线 | 新增端点全部 GET;交易动作族永不进路由(skill 禁区) |
| token 不入前端 | 终端 token 只活在 server 进程(gm.py 既有形态),前端只经 cookie 鉴权 |
| 单源 | 腿目录=`ops.gm_ops_common.LEGS`(active_legs);7002 调用=`gc.api_get`;对照逻辑=`ops.emquant_ab_compare` 的纯函数——presentation 层零业务复制 |
| 向后兼容 | 既有 5 端点加可选 `?leg=`,缺省 `main`——现有前端组件零改动可渐进迁移 |
| 降级不阻断 | 单腿 7002 查询失败→该腿卡片显示「—」/降级态,不炸整个视图(AssetCard 既有「显示 — 而非 0」防线的延伸) |

## 二、后端 API(presentation/server)

### 2.1 `GET /api/v1/gm/legs` —— 腿目录(新增,前端腿选择器/并排卡的唯一数据源)

```json
{"legs": [
  {"key": "main", "label": "主腿", "account_id": "67334fef…", "strategy_id": "d9324346…",
   "strategy_name": "NECK", "role": "incumbent"},
  {"key": "exp",  "label": "实验腿", "account_id": "c4ba3b2e…", "strategy_id": "b24c6a2f…",
   "strategy_name": "NECK-EXP", "role": "challenger"}
]}
```
实现:`gc.active_legs()` + 每腿 `runtime_config(leg_dir)` + 7002 `/v3/strategies` 一次(名字/stage 映射进 leg)。exp 未部署时天然只返回 main(注册表语义,无需特判)。**不在此端点做进程探测**(guard 的职责,5 分钟粒度已进钉钉;Web 端要"在线感"由 overview 的策略 stage 提供)。

### 2.2 既有 5 端点参数化:`?leg=main|exp`(缺省 main)

`overview` 保持全局(策略列表本就含两腿);`asset/positions/orders/trades` 的 `_proxy(path, leg)` 按腿读 `runtime_config(leg_dir)` 取该腿 account。实现即把 `_proxy` 内的 `gc.runtime_config()` 换成按腿解析(单行改动×5)。

### 2.3 `GET /api/v1/gm/ab?date=YYYY-MM-DD` —— 对照数据(结构化)

```json
{"day": "2026-08-29", "ok": false,
 "main":  {"init": {...}, "counts": {"SIGNAL": 8, ...}, "warn_types": {...}},
 "exp":   {"init": {...}, "counts": {...}, "warn_types": {...}},
 "signals": {"added": [], "removed": ["300420.SZ"], "drifted": []},
 "orders":  {"added": [], "removed": [], "drifted": [], "qty_diff": [{"symbol": "…", "incumbent": 60000, "challenger": 100}]},
 "flags": ["信号 diff:…"]}
```
实现前置重构:`emquant_ab_compare.py` 把 `build_report` 拆两半——`compare(day, main_rows, exp_rows, main_acc, exp_acc) -> dict`(纯数据,CLI 与本端点共同消费)+ `render(payload) -> (text, flags)`(文本形态,钉钉/落档不变)。**这是本方案唯一的既有代码重构点**,重构后 `ops/emquant_ab_compare.py` 的 15 个测试必须全绿(纯函数签名不变,只加 compare)。

### 2.4 `GET /api/v1/gm/audit?leg=&date=&event=&limit=500` —— 事件流下钻

数据源=experiments.db `terminal_audit`(已分账户,ingest 15:40 落数)。只读 SQL:`WHERE account_id=? AND ts LIKE day% [AND event=?] ORDER BY ts LIMIT ?`。腿→账户映射经 legs 目录(拒绝跨腿裸 account_id 查询——防手滑拿错账户)。

## 三、前端(presentation/web)

### 3.1 组件与视图变更

```
CockpitView(改造)
├── LegSelector(新,顶栏 segmented:主腿|实验腿,读 /legs,选中态入 localStorage)
├── StatusCard(增强:overview 两腿各一行——策略名+stage+腿标签色)
├── DualAssetCard(新,替代 AssetCard:两腿 nav/可用 并排,列头带腿标签;
│                  差异高亮:实验腿字段缺→「—」降级,不显示 0——虚假繁荣防线沿用)
├── DataHealthCard(不动,全局数据面)
├── TradesTable(参数化:跟随 LegSelector,?leg=)
└── TerminalLogs(不动)

ExperimentView(新,/experiments,导航「实验对照」)
├── RoundCard(轮次状态:轮次类型=校准/候选、candidate 描述、起止日、进度)
├── AbDailyCard(当日对照:/gm/ab → 信号 diff 表 + 两腿漏斗对照条 + flags 红旗区)
├── AbHistoryCard(近 N 日 ok 列表——校准轮"连续绿"进度的可视化)
└── AuditExplorer(事件流下钻:leg/event 过滤 + 表格分页,消费 /gm/audit)
```

### 3.2 轮次档案(RoundCard 数据源)

轮次状态(校准轮/candidate 轮/预登记 diff 预算)目前只活在方案文档与记忆里——落一个轻量档案 `emquant/config/ab_round.json`(手工维护,README runbook 钉死字段):
```json
{"round_id": "calibration-01", "type": "calibration", "candidate": "同参零变量(0.075)",
 "start": "2026-08-29", "expect_diff": "zero", "promote_gate": "10 交易日信号 diff=0"}
```
server 只读透传。这是"多策略实验"的最小可演进形态:未来 candidate 轮换=改此文件,不动代码。

## 四、实施切分

| 阶段 | 内容 | 规模 |
|---|---|---|
| P1 后端 | legs 端点 + 5 端点 ?leg 参数化 + ab_compare 拆 compare/render + /gm/ab + /gm/audit | ~200 行 + 测试 |
| P2 前端 | LegSelector + DualAssetCard + StatusCard 增强 + TradesTable 参数化 | ~250 行 + vitest |
| P3 实验视图 | ab_round.json + ExperimentView 四组件 + 路由/导航 | ~300 行 + vitest |

每阶段独立可交付(P1 后既有 cockpit 不受损;P2 起用户可见多账户;P3 实验可视)。

## 五、测试计划

- 后端:`tests/presentation/` 增 legs/leg 参数化(mock gc.api_get+LEGS)/ab service(复用 ab_compare fixtures)/audit 查询(临时 db);`ops/emquant_ab_compare.py` 拆分后 15 用例全绿(纯函数等价)。
- 前端:`views/__tests__` 增 LegSelector/DualAssetCard(降级态:—而非 0)/ExperimentView 快照;既有 32 用例不破(缺省 leg=main 的兼容断言)。
- 门槛:后端+emquant+前端三套全绿。

## 六、非目标

- 不做交易操作面(下单/启停进 cockpit——只读红线);
- 不做实时推送(SSE/WebSocket——轮询 5s 语义已覆盖观测需求,对照数据日更);
- 不做多终端/多用户(内网单用户既定假设);
- 不把 ab 绩效归一化做进 Web(仿真读数不构成决策证据的认识论纪律——展示工程闸+信号 diff,不展示"实验腿收益更高"这类诱导性对比)。

## 七、风险与边界

| 风险 | 处置 |
|---|---|
| 7002 单腿失败拖慢 cockpit | 每腿独立 4s 超时+降级「—」;legs 端点不聚合 7002 重查询(strategies 一次) |
| ab 数据 15:40 才落库 | AbDailyCard 显示「今日数据 15:40 后更新」占位态;--date 可回看历史 |
| 轮次档案手维护漂移 | README runbook 钉死字段+CI 无(内网)——接受,档案错=展示错,不影响交易链路 |
| 前端旧组件迁移期 | 缺省 leg=main,所有未迁移组件零感知;迁移按组件渐进 |
