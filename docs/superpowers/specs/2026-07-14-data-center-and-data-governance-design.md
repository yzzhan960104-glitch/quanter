# 数据中心 + 数据治理 设计文档（Spec 1/N）

> 系统性重新评估「前后端交互 × 数据维护」的第一份固化 spec。
> 本系列以「四支柱」全局目标架构为上位框架，逐支柱具体化、逐份 spec 落地。
> **本 spec 范围：单一真相源支柱 —— 前端「数据中心」+ 后端「数据治理」**。
> 其余三支柱（任务模型 / 实时事件流 / 后端健康）见后续 spec。

- 立项日期：2026-07-14
- 关联记忆：`quanter-param-training-platform`（Spec1 回测异步化地基已就绪）、`global-architecture-before-details`

---

## 1. 背景与动机

对现有系统做了系统性盘点（34 个后端端点 / 5 个前端页面 / 6 类存储），得出一个根本判断：

> **「同步链路」与「异步链路」在前后端、数据层三处同时并存且互不相通**——这是回测超时、事件循环阻塞、双真相源割裂等所有症状的共同根因。

由此确立**全局目标架构 = 四根支柱**（Karpathy 极简线：不引 Celery / 不引重型 ORM，继续标准库 SQLite + 进程池）：

| 支柱 | 一句话原则 | 本 spec |
|---|---|---|
| ① 任务模型 | 长耗时计算统一抽象为「任务」：提交→进度→结果/可取消 | 后续 spec |
| ② 单一真相源 | 数据分三层，每层只有一个权威存储 + 明确写入者 + 明确生命周期 | **✅ 本 spec** |
| ③ 实时事件流 | 任务进度 / 订单回报走推送（SSE），替代轮询 | 后续 spec |
| ④ 纯净事件循环 | 事件循环只做 IO 编排，CPU/长计算一律卸载；统一异常边界；读写鉴权分离 | 后续 spec |

本 spec 落地**支柱②**：前端把宏观驾驶舱 + 数据湖合并为「数据中心」（单一真相源的前端消费态），后端立「数据治理」四点（单一真相源的后端供给态）。

---

## 2. 设计 A：前端「数据中心」（方案 B 极简版）

### 2.1 路由与导航

- `/dashboard`（宏观驾驶舱）+ `/data`（数据湖）→ 合并为单一「数据中心」路由
- 顶部导航 **5 项 → 4 项**（蔡森筛选 / **数据中心** / AI 复盘 / 实盘中控）
- 默认选中 `macro` 湖（保留「宏观是研究首屏」的习惯）

### 2.2 信息架构：左列表 + 右分流

```
数据中心                              [刷新]
┌──────────────────┬─────────────────────────────────┐
│ 数据集资产表       │ 右栏·按选中湖分流                 │
│ (现状 DatasetTable │                                 │
│  原样保留:         │ 选中 macro  → regime 状态+色带      │
│  名称/源/粒度/     │              + 信贷三因子(驾驶舱①②)│
│  区间/状态/同步)   │ 选中 sector → 板块Top+活跃股池     │
│                  │              (驾驶舱③④)           │
│  ← 点行选中        │ 选中其他湖 → 资产详情卡            │
│                  │   (区间/状态/最新同步/错误+同步按钮)│
└──────────────────┴─────────────────────────────────┘
```

### 2.3 右栏分流策略（关键）

按数据集的「可视化成熟度」分流，**只有 macro/sector 有图表，其余一律表格/详情卡**：

| 选中湖 | 右栏内容 | 来源 |
|---|---|---|
| `macro` | ① regime 状态卡 + 60 日色带；② 信贷三因子折线 | 复用 `DashboardView` ①②（迁移为预览组件） |
| `sector` | ③ 板块资金流 Top 条形；④ 活跃股池表 | 复用 `DashboardView` ③④（迁移为预览组件） |
| 其余湖（daily/minute/crypto/daily_active/fundamentals/north_flow/dragon_list） | 资产详情卡（11 字段 + 同步按钮），**不做图、不抽样** | 现有 `DatasetAsset` 字段，零额外请求 |

→ 驾驶舱四块 = macro/sector 湖的预览。宏观研究动线变成「点 macro 看宏观全景（带数据新鲜度 + 一键同步），点 sector 看板块全景」——既解决「驾驶舱悬空」，又让驾驶舱内容有了数据底座归属。

### 2.4 后端契约：零增量

- **不要** preview 端点；**不要** 给 `DatasetAsset` 加字段
- 复用现有 `GET /data/datasets` + macro 的 `regime/credit/sector/flow` 端点
- 其余湖右栏的「资产详情卡」信息全在 `DatasetAsset` 现有 11 字段里，零额外请求

### 2.5 迁移清单

| 现有 | 去向 |
|---|---|
| `DashboardView.vue` 四块图表逻辑 | 迁移为「湖预览组件」（macro 预览 + sector 预览） |
| `DataLakeView.vue` 的 datasets 拉取 / syncing 3s 轮询 / 同步触发 | 迁入新数据中心页（左栏列表 + 右栏同步按钮） |
| `DatasetTable.vue` | 保留为左栏内部渲染（行点击 → emit select） |
| 路由 `/dashboard`、`/data` | 合并保留路径 `/data`（语义改为「数据中心」），删 `/dashboard` |

### 2.6 决策记录（ADR）

- **形态选 B（数据集为中心）而非 A（驾驶舱为主+数据底栏）**：用户要的是把数据湖「升级」为数据中心，而非把驾驶舱「保住」只加底栏。B 让数据资产成为组织主轴。
- **B 类湖不做可视化（保持表格）**：用户明确「数据湖不需要做图，保持现状表格」。极简优先，不为 9 个湖各手搓图表。
- **路由合并为一项**：用户确认。接受「一屏看全宏观 → 点 macro/sector 两个湖看全」的取舍，换取每次看宏观都带数据新鲜度上下文 + 一键同步。

---

## 3. 设计 B：后端「数据治理」（四点）

### 3.1 问题诊断（来自实战 + 代码确认）

用户提出两个核心痛点，根因同源——**数据层缺统一治理，每个湖独立同步、独立定起点、独立选源**：

- **区间一致性**：各湖起点不一（2016 vs 2024）、终点不一（到最近 vs 到 3 月）。
  - 根因：各 sync 脚本 `--years` 各自为政、无全局基准日；无同步编排器；`_derive_status` 标 stale 仅 UI 徽章不强制重同步；回测不感知区间参差。
  - 量化后果：**「名义区间」≠「有效区间」**（晚上市标的被默默跳过，年化/胜率基于短区间）；多湖联立只能取最短公共区间浪费数据；宏观择时用旧 macro 信号滞后。
- **源冗余**：OHLCV 同一份数据三套口径——data_lake daily 湖（**前复权**）/ `TushareDataFetcher` 在线（**不复权** `pro.daily`）/ parquet 缓存 / Mock。
  - 后果：前复权 vs 不复权价格不可比；限频/熔断/前视/缓存每源重写；下游易混用出错。

### 3.2 治理一：单一源策略

A 股/ETF/宏观的权威源定为 **Tushare（10000 积分，tnskhdata 代理，多 token 轮询）**，落 data_lake 湖：

- 日线 OHLCV：daily + adj_factor 重建前复权（现状，保留）
- 基本面/财务：daily_basic + fina_indicator + 新增 income/balancesheet/cashflow 三大报表
- 宏观：切 Tushare cn_m/cn_cpi/cn_ppi/cn_gdp/cn_pmi + shibor（替代 AKShare）；社融/DR007 Tushare 无则 akshare fallback
- 资金流/龙虎榜/融资融券/北向/板块/指数/特色筹码/股东：新增 Tushare 接口采集（设计 C）
- ETF：fund_basic/fund_daily/fund_nav/fund_portfolio/fund_share
- AKShare 宏观/板块退役（切 Tushare）；FRED 保留为美国宏观补充；JQData 仅保留分钟级
- MockDataFetcher 保留为测试兜底，不进生产数据流

### 3.3 治理二：统一数据契约 / 目录

新增一个**声明式数据目录**（config 层），每个数据集定义：

```
key / 权威源 / 统一起点年 / 更新节奏 / 复权口径 / schema / freshness_hours
```

所有 sync 脚本与 `DATASET_REGISTRY` 向此契约对齐，而非各自硬编码 `--years`。契约是「数据集该长什么样」的单一真相，sync 脚本只是「把数据弄成契约那样」的执行体。

### 3.4 治理三：对齐基准

- **起点对齐**：全湖统一一个 `start_year`（契约定义），不足的标的标记「上市后才有」（区间透明，见下）。
- **终点对齐（新鲜度）**：新增**统一同步编排器**——一次同步把所有湖推到同一截止日；`stale` 状态**自动触发重同步**（而非仅 UI 徽章）。
- 替代当前「各湖独立 sync + 启动 sweep + 手动点同步」的松散模式。

### 3.5 治理四：区间透明

- 每湖 / 每标的记录**实际 `[start, end]`**（`DatasetAsset.data_start/data_end` 已有，需在契约/编排层保证其准确且对齐）。
- 回测**显式报告有效区间与样本量**：不再假装回测了完整区间；晚上市标的的缺失段被明确标注，统计基于真实有效区间。
- 前端「数据中心」右栏详情卡顺势成为**数据质量看板**：直观展示对齐缺口（起点太晚 / 终点太旧）。

### 3.6 决策记录（ADR）

- **单一源（非多源交叉校验）**：用户确认「同一份数据一个可靠源就够」。极简优先，多源带来的口径不一致与维护负担大于交叉校验的收益。
- **统一对齐基准（非各湖按特性各自起点）**：用户确认。宁可舍弃部分湖的早期数据，也要保证跨湖可比、回测区间可信。

### 3.7 Tushare 三大类全量采集（单一源落地 · 设计 C）

**目标**：用 Tushare 10000 积分账号（tnskhdata 代理；常规 500 次/分、特色 300 次/分、常规总量无上限）把**股票 / ETF / 宏观**三大类所有可获取数据采入 data_lake 湖。

**决策记录**：
- **权威源 = Tushare（非 JQData）**：用户纠正——10000 积分是 Tushare 账号。Tushare 已是日线/基本面源，**不用切源，扩展采集范围**（此前误判为 JQData，已回改）。
- **范围 = 股票高价值 + ETF专题 + 宏观**（不含期货/期权/港股/美股，YAGNI）。
- **退市股不纳入**：保持 list_status 在售。生存者偏差盲区暂留 TODO。
- **限流复用**：`_tushare_compat` 多 token 轮询 + `tushare_rate_limiter` 令牌桶 + `tushare_breaker` 熔断 + shard 断点续传。

**架构：通用同步器 + 声明式配置注册表**（落地治理二「声明式契约」）：
- `data/tushare_sync.py` 通用同步器（配置驱动：接口/字段/分页 by=symbol|date|single/落湖/index_mode）
- `config.py` 的 `TUSHARE_DATASETS` 声明式注册表：每数据集一条配置
- 各 sync 脚本薄封装调 `sync_dataset(key, ...)`

**三大类采集清单**（详见三份 plan）：
- **股票**：财务三大报表(income/balancesheet/cashflow，ann_date 防前视) + 预告/快报/分红 + 资金流 moneyflow + 龙虎榜 top_list/top_inst + 融资融券 margin* + 北向 hsgt* + 板块 concept/ths + 指数 index_* + 特色筹码 cyq_perf(300/分) + 股东/解禁/停牌
- **ETF**：fund_basic(market=EFT) + fund_daily + fund_nav + fund_portfolio + fund_share + 指数成分(共用)
- **宏观**：cn_m(M0/M1/M2→M1M2_gap) + cn_cpi/ppi/gdp/pmi + shibor + 交易所统计 szse/sse_daily；社融/DR007 走 akshare fallback（Tushare 无专门接口）

**CreditRegime 不变量**：宏观切 Tushare 后，macro 湖必须含 `shrzgm` + `M1M2_gap` 列（`core/macro_regime.py:154`），CreditRegime 代码不改。

**额外收益**：单一源 → 解决「OHLCV 三套口径」源冗余；扩展采集覆盖此前缺失的 30+ 接口。

**实施**：拆三份 plan（A 股票/B ETF/C 宏观），执行序 A→B→C（B/C 依赖 A 通用同步器）。

---

## 4. 范围外（YAGNI / 后续 spec）

本 spec **不包含**以下，留待后续：

- **任务模型支柱**（回测/扫描/AI/同步统一异步任务化、前端接入 `/replay/async`、进度/取消）——根本矛盾，下一份 spec。
- **实时事件流**（SSE 接入前端，替代实盘 2s 轮询）。
- **后端健康**（scan/replay 投线程池救事件循环、`/connect` 超时、全局异常 handler、读写鉴权分离、清僵尸 celery/哨兵）。
- **B 类湖可视化 / preview 端点**（设计 A 明确不做）。
- **数据质检层**（OHLC 关系 / 负值 / NaN 比例校验 + 可信度元信息）——数据治理的自然延伸，首版后置避免范围膨胀。
- **生存者偏差**（退市股纳入回测）：用户本次决策**不纳入**（保持仅在售股）。JQData `get_all_securities` 已具备退市股能力，后续可开关式开启。记 TODO。
- **入库零质检**：OHLC 关系/负值/NaN 校验缺失，留待数据质检层。
- **前复权基准一致 / 可复现性**：Tushare `daily+adj_factor` 手动重建前复权，基准随同步时点漂移（resume 断点续传加剧跨标的基准不一致）。留待后续 spec（冻结基准日或服务端复权源）。

---

## 5. 实现顺序建议

1. **前端「数据中心」**（设计 A）：零后端依赖，可先落地见效，立即解决驾驶舱悬空 + 导航臃肿。
2. **JQData 全面接入**（设计 C / 治理一落地）：扩展 jqdata_client（日线/基本面/宏观/标的）+ 配额自适应 + 重写 sync_data_lake/sync_fundamentals/sync_macro_credit 走 JQData + CreditRegime 适配 + 修正 DATASET_REGISTRY 文档债。**这是数据治理的前提**（单一源先立，再谈对齐）。
3. **统一数据契约 / 目录**（治理二）：契约层对齐 JQData 单一源。
4. **对齐基准 + stale 自动重同步编排器**（治理三）：依赖契约。
5. **区间透明**（治理四）：回测报告增强 + 前端详情卡升级为质量看板。

每步可独立验证、独立提交。

---

## 6. 验收标准

**设计 A（前端数据中心）**：
- 导航 4 项；`/dashboard` 已删；`/data` 为数据中心。
- 选中 macro 显示 regime 状态+色带+三因子；选中 sector 显示板块 Top+活跃股池；选中其他湖显示资产详情卡 + 同步按钮。
- syncing 3s 轮询起停逻辑保留；驾驶舱四块图表无回归。
- E2E：新增数据中心页动线测试（参考 `tests/e2e/caisen_replay_tab.py` 风格）。

**设计 B（数据治理）+ 设计 C（JQData 接入）**：
- jqdata_client 新增 fetch_daily_bars/fetch_fundamentals/宏观/标的 方法；配额运行时自适应（不硬编码 95 万）。
- sync_data_lake/sync_fundamentals/sync_macro_credit 改走 JQData；daily 湖前复权由 JQData `fq='pre'` 服务端保证（无手动重建基准漂移）。
- CreditRegime 适配聚宽宏观字段（缺指标单指标 fallback AKShare）。
- DATASET_REGISTRY source 字段与实际一致（daily/fundamentals/macro=JQData）。
- 数据契约目录存在且各 sync 脚本对齐；各湖 `data_start/data_end` 在数据中心页可视化对齐。
- OHLCV 无「前复权 vs 不复权」口径冲突（单一源 JQData）。
- 回测报告含「有效区间 / 样本量」字段，不再假装完整区间。
- stale 能被自动重同步（编排器），而非仅徽章。

---

## 7. 附录：端点级诊断 TODO（降级细节，后续支柱实现时回查）

来自系统性盘点的分级诊断，本 spec 未直接处理，留待对应支柱 spec 回查落位：

- **前端**：F1 回测/AI 同步阻塞无进度；F2 实盘 2s 串行轮询；F3 跨页状态缺口（无 Pinia）；F4 facade timeout 散落；F5 组件复用低；F6 高危页 E2E 0 覆盖。
- **数据**：D1 回测历史 JSON/SQLite 双源割裂；D2 parquet 缓存 key 烧 start/end + 无 GC；D3 data_lake 820MB+ 常驻内存双副本；D4 plans/cooldown JSON 老化；D5 并发写 SQLite 非原子。
- **后端**：B1 scan/replay 阻塞事件循环；B2 异步回测链路前端零接入；B3 `/connect` 无超时；B4 SSE 前端零消费；B5 celery_app 僵尸；B6 启动 sweep 哨兵残留；B7 鉴权无读写分离；B8 异常转译不一致。
