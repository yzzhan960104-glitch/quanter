# T5 · data 层完整性根治方案调研

> 工单：`plans/wayfinder/T5.md`（type=research，2026-08-08）
> 主流程：`mattpocock-skills:research`
> 信源：`data/` 全模块源码 + `config/data.py`/`registry.py` + `ops/data_pipeline.py` + `data_lake/` parquet 实样 + memory 三份（`data-lake-integrity-gap` / `akshare-jqdata-retired` / `c9-data-observation-remediation-status`）+ `docs/data-source-of-truth.md` / `docs/data_pool.md`
> 约束：全中文 + 只调研不改代码 + 每条结论有信源支撑，存疑标「待实证」

---

## 0. 结论速览（TL;DR）

- **最关键缺口一句话**：生产数据链 supervisor（`ops/data_pipeline.py`）的完整性 gate 是 `data/freshness.py`——它**只校验「最新日期是否=期望交易日」（实时性）**，**完全不校验历史连续性 / 停牌解释 / 复权一致性**；真正能扫历史缺口的 `scan_integrity.py` + `repair_gaps.py` 是**孤立 CLI，无任何调度/cron 触发**。结果：lake 历史段漏采（300214.SZ 案例那种）只能在信号产出最后一道 `filter_universe_by_continuity`（fail-open 窗口 gate）被「跳过该标的」被动兜底，缺口本身永远不被发现、不被补采。
- **根治核心**：把 `scan_integrity` 升级为**生产链路强制 gate**（接进 `data_pipeline` T1/T2 + 每周全扫），把 `repair_gaps` 升级为**自动补采触发**（scan FAIL → 自动 repair，受配额/熔断守卫），并把零散的完整性维度收敛到 `data/integrity.py` 单一模块（新增 freshness 没覆盖的维度：历史连续性、复权断崖、跨湖时区）。
- **活数据债（最高优先级，待实证）**：当前工作区 `data_lake/a_shares_daily.parquet` **仅 3200 行 = 单日（2026-07-24）× 3200 标的**（git status 标记 `M`，commit 历史显示它本是 LFS 入库的 discovery 快照，memory `data-lake-integrity-gap` 记录补采后应有 10163190 行）。若此状态进入生产，`find_gaps` 在单日湖上返回空（每个标的 `[actual_min, actual_max]` 区间只有 1 天，无缺口可找），**三层完整性 gate 全部静默失效**——这是比 300214 历史漏采更致命的活病灶。

---

## 1. 现状盘点（7 模块职责 + 数据流衔接 + 缺口分布）

### 1.1 模块职责矩阵

| 模块 | 行数 | 职责（一句话） | 关键导出 | 数据流位置 |
|------|------|----------------|----------|------------|
| `data/fetcher.py` | 1291 | **回测/服务侧取数抽象**（DataFetcher 类族：DataFetcher/MockDataFetcher/FredDataFetcher/TushareDataFetcher/CompositeDataFetcher），提供 `fetch_ohlcv/fetch_macro/fetch_factor_data` 接口，走 API+parquet 缓存 | 5 个 Fetcher 类 | 回测/组合服务读，**不直接落湖** |
| `data/tushare_sync.py` | 634 | **通用 Tushare 湖同步器**（配置驱动），`_fetch_with_guard` 限频+熔断+三态分类 → 分页（by=symbol/date/single）→ shard 断点续传 → `_build_multiindex` 落湖 | `sync_dataset` / `_fetch_with_guard` / `_sync_by_symbol/date/single` | 采集层（写湖） |
| `data/integrity.py` | 296 | **完整性算法库**（纯函数）：`load_suspend_intervals`（S/R→停牌日集）/ `fetch_trade_days`（交易日基准）/ `find_gaps`（全市场连续性扫描）/ `check_window_continuity`（窗口 gate）/ `filter_universe_by_continuity`（universe 级 gate） | 4 个算法函数 + GapRange/ContinuityResult | 被动调用（不触网/不读盘，入参注入） |
| `data/resilience.py` | 306 | **容灾基建**：CircuitBreaker（熔断三态）+ RateLimiter（令牌桶）+ 模块级单例（tushare 双桶 500/300 per min + akshare/fred） | CircuitBreaker / RateLimiter + 6 个单例 | 被 `_fetch_with_guard` 等消费 |
| `data/lake_reader.py` | 309 | **只读多湖缓存**（DataLakeReader 单例）：load parquet→内存，tz 归一+sort+价格 ffill，提供 `get_cross_section`/`get_timeseries` | DataLakeReader | 读取层（服务/策略读湖） |
| `data/tools/sync_daily_incremental.py` | 298 | **daily 湖日频增量同步器**（独立于 tushare_sync 主管道）：按日分页拉 raw daily + adj_factor → 前复权 → append + dedup → 除权检测 `_recompute_symbol` + 近期回扫 `_backscan_recent` | `sync_daily_incremental` / `_recompute_symbol` / `_backscan_recent` | 采集层（写 daily 湖，生产调度入口） |
| `data/tools/sync_incremental.py` | 387 | **quick 批日频增量编排**（配置驱动数据集：moneyflow/margin/suspend_d/etf 等）：读 d0→拉 [d0+1,today]→merge dedup；含 DAILY_SYMBOL_KEYS（fund_*/cyq_*）+ 周期条年龄守卫 | `sync_one_key` / `main` | 采集层（写配置驱动湖） |

辅助工具：
- `data/tools/scan_integrity.py`（146）= 全市场连续性扫描 CLI（规则 1），调 `integrity.find_gaps`，返 JSON 报告，退出码 0/1 可作 gate。
- `data/tools/repair_gaps.py`（178）= 漏采补采 CLI（规则 3），复用 `sync_daily_incremental._fetch_paged`，前复权 + dedup keep last。
- `data/tools/run_data_check.py`（178）= **生产 T1/T2 检查点入口**，调 `freshness.check_freshness`（不是 integrity），T2 FAIL 重采→熔断 eod。
- `data/freshness.py`（88）= 实时性检查核心，比 parquet 最新日 vs 期望交易日。
- `data/tools/sync_data_lake.py`（210）= daily 湖**全量初始化**（按标的轮询，~2.8h，首次/重置用）。

### 1.2 数据流衔接图（采集 → lake → 读取 → 信号）

```
┌─────────────── 采集层（写湖）───────────────┐
│ ops/data_pipeline.py (supervisor, 3 步串行)  │
│   ① run_data_check t1  ── freshness.py       │  ← 生产 gate #1（只查实时性）
│   ② sync_daily_incremental.py ──→ daily 湖   │  ← _recompute_symbol + _backscan_recent
│   ③ run_data_check t2  ── freshness.py       │  ← 生产 gate #2（实时性 + 重采 + 熔断）
│                                              │
│ sync_incremental.py (quick 批 cron) ──→ 各湖 │  ← moneyflow/margin/suspend_d/etf/...
│ sync_all_tushare.py (手动全量)     ──→ 各湖  │  ← 含 TUSHARE_DATASETS["daily"]（双轨！）
│ scan_integrity.py (孤立 CLI)       ──→ JSON  │  ← ❌ 无调度触发
│ repair_gaps.py (孤立 CLI)          ──→ daily │  ← ❌ 无调度触发
└──────────────────────────────────────────────┘
                    ↓ parquet
┌─────────────── 读取层（只读）────────────────┐
│ DataLakeReader (lake_reader.py)              │  ← ❌ load 时零完整性校验（只 tz/sort/ffill）
│ fetcher.py (DataFetcher 类族，回测用)         │
└──────────────────────────────────────────────┘
                    ↓ DataFrame
┌─────────────── 信号层（消费）────────────────┐
│ trading/engine._eod                          │
│   └ filter_universe_by_continuity ──→ scan_live │ ← 生产 gate #3（窗口连续性，fail-open）
│ backtest/replay._apply_continuity_filter     │  ← 回测 gate（同源 filter）
└──────────────────────────────────────────────┘
```

### 1.3 缺口分布（结构性问题）

| # | 缺口 | 证据 |
|---|------|------|
| G1 | **daily 湖双轨制**：`TUSHARE_DATASETS["daily"]`（registry.py:707, by=symbol+adj_api）走 tushare_sync.sync_dataset（slow 批），与独立 `sync_daily_incremental.py` **写同一个 `a_shares_daily.parquet`**。日常不冲突（slow 批不在 cron），但任何人跑 `sync_all_tushare --batch slow` 或 `sync_incremental` 含 daily key 会覆盖。 | registry.py:707 + sync_daily_incremental.py:39 `LAKE` |
| G2 | **scan_integrity / repair_gaps 无调度**：grep 全 repo 无 `.bat`/schtasks/cron 调用它们，只在文档/测试/自身 CLI 出现。memory `data-lake-integrity-gap` 说"5 条规则已实施"，但规则 1（扫描）+规则 3（补采）**只是 CLI 工具，未接生产链路**。 | grep `scan_integrity\|repair_gaps` 全 repo |
| G3 | **生产 gate 维度单一**：`run_data_check` T1/T2 只调 `freshness.check_freshness`，仅比 `latest_date >= expected_date`，不查历史连续性/停牌/复权。 | freshness.py:80 `if latest >= expected_date` |
| G4 | **完整性 gate 全部 fail-open**：`filter_universe_by_continuity` 在 `trade_days` 空集时 `expected` 恒空→`ok=True` 全放行；`replay._apply_continuity_filter` 整体 try/except 包裹，filter 自身异常也放行。 | integrity.py:273-274 注释 + replay.py:133-136 |
| G5 | **lake_reader load 时零完整性校验**：`_load_impl` 只做 tz 归一 + sort + 价格 ffill，不检查缺口/停牌/列完整。 | lake_reader.py:120-192 |
| G6 | **活数据债（待实证）**：当前工作区 `a_shares_daily.parquet` 仅 3200 行 = 单日 2026-07-24 × 3200 标的。git status 标记 `M`，commit 历史（c242b5f3）显示它应刷新至 08-04。memory 记录补采后应 10163190 行。 | 实测 `pd.read_parquet` + git log + git diff --stat |

---

## 2. 已知缺口核实（memory `data-lake-integrity-gap`）

### 2.1 memory 记录（11 天前，2026-07-27）

- **300214.SZ 案例**：lake 缺 07-14~07-21（5 天真实交易），残缺数据识别颈线 8.08 → 07-24 close 11.86 误判突破产计划；补采后重跑 detect 返 None（正确）。**核实：属实**，算法逻辑在 integrity.py 的 `find_gaps` + `check_window_continuity` 中复现，与 memory 描述一致。
- **根因**：`sync_daily_incremental` 只从 d0 往后增量，不补 d0 之前缺口；停牌复牌段若某次 sync 时 Tushare 没出/分页漏，lake 永久缺这段。**核实：属实**，`sync_daily_incremental.py:154` `d0 = str(...max().date())` 只看最新日。
- **方案落地（memory 称 5 条规则全实施）**：
  - 规则 1（扫描）→ `data/integrity.py:find_gaps` + `data/tools/scan_integrity.py` ✅ 代码存在
  - 规则 2（停牌识别）→ `data/integrity.py:load_suspend_intervals` ✅ 代码存在
  - 规则 3（补采）→ `data/tools/repair_gaps.py` ✅ 代码存在
  - 规则 4（窗口 gate）→ `strategies/neckline_method.scan_live` → **已上提到** `data/integrity.filter_universe_by_continuity`（Task 7 U5），接入 `engine._eod:2704` + `replay:130` ✅ 代码存在且接入
  - 规则 5（回扫）→ `sync_daily_incremental._backscan_recent` ✅ 代码存在
  - **但**：规则 1/3 是孤立 CLI，**无调度触发**（G2）；规则 5 只 `logger.warning` 告警，**不触发 repair_gaps**（sync_daily_incremental.py:266 仅打提示 "跑 repair_gaps --auto 补"）。

### 2.2 新发现（本次调研，memory 之后 11 天的演变）

- **G6 活数据债**：当前 `a_shares_daily.parquet` 仅单日 3200 行（见 §0）。这与 memory「补采后 10163190 行」严重背离。可能原因：①git LFS 指针未拉取（.gitignore line 88 `!data_lake/a_shares_daily.parquet` 是 LFS 例外）；②某次测试/操作覆盖写；③discovery 快照机制（commit message 称"discovery 新快照"）故意缩窗口。**待实证**：需用户确认这是 LFS 问题还是真数据债。
- **find_gaps 在单日湖失效**：`find_gaps` 用 `[actual_min, actual_max]` 区间扫缺口（integrity.py:162-163），单日湖每标的区间只有 1 天，`expected_sorted` 只有 1 天且 in actual，**返回空 gaps**——scan_integrity 在此湖上会报「无漏采 PASS」，**完整性 gate 静默失效**。这是 G6 的连锁放大效应。

---

## 3. 停牌识别（suspend_d 接口覆盖 + 调用点 + 补采联动）

### 3.1 suspend_d 数据集配置（registry.py:413-431）

```python
"suspend_d": {
    "api": "suspend_d", "by": "date",
    "date_col": "trade_date", "symbol_col": "ts_code",
    "fields": "ts_code,trade_date,suspend_timing,suspend_type",
    "lake": "data_lake/suspend_d.parquet",
}
```
- **采集**：by=date（逐交易日全市场），走 `sync_incremental.py` quick 批（classify 把 by≠symbol 归 quick），**已在生产 cron**。
- **降级事实（registry 注释 line 417-423）**：API 仅返 4 列，**不返 ann_date**（公告日），只能用 `trade_date`（停牌当日）作索引——存在轻微前视残留（停牌当日盘前可能未感知），但停牌通常盘前即时公告，可接受。
- **实测局限**：`suspend_timing` 99% NaN，`suspend_type` 是主有效字段（S=停牌/R=复牌）。

### 3.2 落湖实测（本次调研）

```
data_lake/suspend_d.parquet: 13571 行, index=[date, symbol], cols=[suspend_timing, suspend_type]
suspend_type: {S: 12148, R: 1423}
date range: 2023-07-17 → 2026-08-06（最新）
```
**结论：suspend_d 采集链路健康**，覆盖近 3 年，最新至 08-06（T-2）。

### 3.3 调用点（integrity.py 的消费）

- `integrity.load_suspend_intervals(suspend_df, trade_days_set)`：S/R 事件 → per-symbol 停牌交易日集合（integrity.py:57-100）。**纯函数，入参注入**。
- 上游调用：`scan_integrity.py:82` / `sync_daily_incremental._backscan_recent:261` / `engine._load_integrity_ctx:421`（engine.py:405 注释）/ `replay._apply_continuity_filter:123`。
- **联动**：`find_gaps` / `check_window_continuity` 都用 `suspend_intervals` 区分「合法跳空（停牌）vs 漏采」——`_build_gap_range` 整段全在停牌集则 `suspend_justified=True`（integrity.py:183-184）。

### 3.4 缺口

- **补采联动断链**：`repair_gaps.py` 补采时**不查询 suspend_d**（只按 `find_gaps` 输出的 unjustified gaps 补），而 `find_gaps` 已用 suspend_d 过滤掉停牌段——逻辑上自洽，但若 suspend_d 本身漏采（停牌事件未落湖），`find_gaps` 会把停牌段误判为漏采，`repair_gaps` 去 Tushare 拉 daily 会拉到空（停牌日本就无行情）→ 补采空写。**无校验**：repair_gaps 拉空只 warning 不重试也不标位（repair_gaps.py:78）。
- **suspend_d 自身完整性无 gate**：若 suspend_d 湖某日漏采，无人发现（freshness 只查最新日，不查历史完整）。

---

## 4. 补采机制（d0 前缺口修复现状 + 触发条件 + SOP）

### 4.1 现状：三层补采，全手动

| 层 | 脚本 | 触发 | 覆盖 | 缺陷 |
|----|------|------|------|------|
| 日频增量 | `sync_daily_incremental.py` | schtasks 自动（ops/data_pipeline ②） | d0+1 → today | 不补 d0 前缺口 |
| 近期回扫 | `_backscan_recent`（规则 5） | sync_daily_incremental 内部自动 | 近 30 交易日 | **只 logger.warning，不触发 repair**（sync_daily_incremental.py:266 仅打提示文字） |
| 全市场扫描+补采 | `scan_integrity.py` + `repair_gaps.py` | **纯手动** | 全历史 | **无调度**（G2） |

### 4.2 触发条件（手动 SOP，来自 memory + CLI docstring）

```bash
# 1. 全市场扫描（退出码 0=PASS / 1=有漏采）
python -m data.tools.scan_integrity --since 2024-01-01 --end 2026-08-08 --report logs/integrity.json

# 2. 补采（按报告 或 --auto 内部 scan）
python -m data.tools.repair_gaps --report logs/integrity.json
python -m data.tools.repair_gaps --auto --since 2024-01-01 --end 2026-08-08
python -m data.tools.repair_gaps --symbol 300214.SZ --auto --dry-run  # 单标的诊断

# 3. 验证（重跑 scan 应无漏采）
python -m data.tools.scan_integrity --since 2024-01-01 --end 2026-08-08
```

### 4.3 缺陷清单

- **C1**：回扫发现漏采不自动补（sync_daily_incremental.py:264-268 只 `_log.warning` + 文字提示），需人工跑 repair_gaps。
- **C2**：scan/repair 无 cron/schtasks，依赖人记忆。
- **C3**：repair_gaps 前复权基准用「缺口段窗口最新 adj」（repair_gaps.py:87-93 注释），**不重算除权标的全历史 qfq 基准**——memory `data-lake-integrity-gap:46` 明确标为 follow-up，补采段与历史段基线可能不一致（除权断崖）。
- **C4**：repair_gaps 拉 daily 空响应只 warning 不重试不标位（repair_gaps.py:78），后续无法区分「真无数据」vs「接口瞬时故障」。
- **C5**：无 idempotency 审计：repair 写湖后无校验「补采后 find_gaps 是否归零」（memory 记录用户手动验证过一次，但非自动化）。

---

## 5. 完整性校验 gate（现有维度 + 缺失维度 + 链路位置）

### 5.1 现有 gate 三层（维度互不重叠）

| 层 | 位置 | 维度 | 触发 | 行为 |
|----|------|------|------|------|
| **Gate #1 实时性** | `run_data_check` T1/T2 → `freshness.check_freshness` | latest_date >= expected_date | schtasks 自动（17:00/18:30） | T2 FAIL → 重采→熔断 eod |
| **Gate #2 窗口连续性** | `engine._eod:2704` / `replay:130` → `filter_universe_by_continuity` | 识别窗口内有无未解释漏采（停牌放行） | eod/replay 自动 | fail-open：不通过→跳过该 symbol 不产信号 |
| **Gate #3 全市场连续性** | `scan_integrity.py`（孤立 CLI） | 全历史连续性 + 停牌解释 | **无自动触发** | 退出码 0/1，可作 CI gate 但未接 CI |

### 5.2 现有维度的盲区

| 维度 | Gate #1 | Gate #2 | Gate #3 | 负责模块 | 状态 |
|------|---------|---------|---------|----------|------|
| 实时性（今日数据到没到） | ✅ | ❌ | ❌ | freshness.py | 已接生产 |
| 历史连续性（窗口内漏采） | ❌ | ✅ | ✅ | integrity.py | Gate#2 接生产，Gate#3 孤立 |
| 历史连续性（全期漏采） | ❌ | ❌（只查窗口） | ✅ | integrity.py | **❌ 无生产 gate** |
| 停牌解释（合法跳空 vs 漏采） | ❌ | ✅ | ✅ | integrity.py + suspend_d | 同上 |
| 复权一致性（除权断崖） | ❌ | ❌ | ❌ | sync_daily_incremental._recompute_symbol | **❌ 完全无 gate**（只在采集时重算，不校验结果） |
| 时区对齐 | ❌ | ❌ | ❌ | lake_reader._normalize_and_sort | load 时归一，无事后校验 |
| 列完整性（缺列/全 NaN 列） | ❌ | ❌ | ❌ | 无 | **❌ 完全无 gate** |
| 跨湖一致性（daily vs suspend_d 日期对齐） | ❌ | ❌ | ❌ | 无 | **❌ 完全无 gate** |
| parquet 物理健康（文件大小/可读性） | ❌（freshness 读失败会 FAIL） | ❌ | ❌ | freshness.py（间接） | 部分覆盖 |

### 5.3 gate 在链路中的位置（关键洞察）

```
采集(sync) ──→ lake(parquet) ──→ 读取(reader) ──→ 信号(engine/replay)
   │              │                  │                │
   │              │                  │                ├─ Gate#2 窗口连续性（fail-open）
   │              │                  │                │   ↓ 只跳过 symbol，缺口仍在湖里
   │              │                  │
   │              │                  └─ lake_reader load：零完整性校验（G5）
   │              │
   │              └─ ❌ 无事后完整性 gate（scan_integrity 孤立）
   │
   └─ Gate#1 实时性（run_data_check T1/T2）
       └─ T2 FAIL 重采只调 sync_daily_incremental + freshness，不调 scan/repair
```

**核心问题**：完整性 gate（Gate #2）放在信号产出最后一道，是「带病运行的兜底」——它让残缺数据不产误信号（300214 教训的临时缓解），但**缺口本身永远不被发现、不被修复**。lake 持续带病，每轮 eod 都要重跑一次 filter（性能浪费），且新策略/新窗口可能落到没被 filter 覆盖的缺口段。

---

## 6. 根治方案设计

### 6.1 设计原则

1. **完整性 gate 前置到采集层**：缺口在进 lake 前被发现，而不是进信号层才兜底。
2. **scan + repair 自动化**：消除手动 CLI 依赖，scan FAIL → 自动 repair（受配额/熔断守卫）。
3. **维度收敛**：所有完整性算法归 `data/integrity.py` 单一模块，gate 调用统一接口。
4. **不破坏现有 fail-open 语义**：信号层 gate 保留作最后一道防线，但不再是唯一防线。
5. **daily 湖双轨收口**：消除 `TUSHARE_DATASETS["daily"]` 与 `sync_daily_incremental` 的双写源。

### 6.2 校验维度全集（目标态）

| 维度 | 实现 | 触发时机 | FAIL 动作 |
|------|------|----------|-----------|
| D1 实时性 | `freshness.check_freshness`（已有） | T1/T2（已有） | 重采→熔断 eod（已有） |
| D2 全期连续性 | `integrity.find_gaps`（已有） | **新增**：每周全扫 + 每日 scan 增量段 | 自动 repair_gaps |
| D3 停牌解释 | `integrity.load_suspend_intervals`（已有，集成在 D2） | 同 D2 | 同 D2 |
| D4 窗口连续性 | `integrity.filter_universe_by_continuity`（已有） | eod/replay（已有） | 跳过 symbol（已有，保留） |
| **D5 复权一致性** | **新增** `integrity.check_adj_consistency`：扫 daily 湖每标的 adj_factor 单调性 + 除权日断崖检测 | 每周全扫 | 触发 `_recompute_symbol` 全量重算 |
| **D6 列完整性** | **新增** `integrity.check_columns`：daily 湖 OHLCV 列存在 + 非全 NaN + 数值范围（如 high>=low） | 每日 T2 后 | 告警 + 标记坏段 |
| **D7 跨湖时区/日期对齐** | **新增** `integrity.check_cross_lake_dates`：daily 与 suspend_d/trade_cal 的日期域子集关系 | 每周 | 告警 |
| **D8 parquet 物理健康** | `freshness` 已间接覆盖（读失败 FAIL） | T1/T2 | 已有 |

### 6.3 自动补采触发设计

```
┌──────────────────────────────────────────────────────────┐
│ ops/data_pipeline.py 增强（在 T2 后追加步骤 ④⑤）          │
│                                                          │
│ ④ integrity_scan（每日，只扫近 30 交易日增量段）           │
│    └─ scan_integrity.scan(since=today-45d, end=today)    │
│    └─ FAIL(有unjustified) → 自动 repair_gaps --auto       │
│    └─ repair 后重 scan 验证 → 仍 FAIL → 告警（不熔断 eod） │
│                                                          │
│ ⑤ integrity_full_scan（每周/手动，全历史）                 │
│    └─ scan_integrity.scan(全期)                          │
│    └─ FAIL → 生成报告 + 钉钉告警（人工评估，历史段补采慢）  │
└──────────────────────────────────────────────────────────┘
```

**配额守卫**：repair_gaps 受 `_fetch_with_guard` 限频（已复用 sync_daily_incremental._fetch_paged），除权重算受 `_recompute_symbol` 的 try/except 不阻断整批（sync_daily_incremental.py:233-236 已有）。新增的 D5 复权一致性检测若触发批量重算，应走 schtasks 独立时段（避免抢占日频采集配额）。

### 6.4 修复 SOP（自动化后的运维兜底）

| 场景 | 自动动作 | 人工兜底 |
|------|----------|----------|
| 日频增量漏采（d0 前 30 天内） | 步骤④自动 repair | repair 失败 3 次→钉钉告警→人工跑 `repair_gaps --auto --since ... --end ...` |
| 全历史漏采（30 天前） | 步骤⑤周扫告警 | 人工评估范围 + 跑 `repair_gaps --auto`（慢，按段补） |
| 除权断崖（D5） | 检测到→标记标的→`_recompute_symbol` 重算 | 重算失败→人工跑 `sync_data_lake.py --symbols <list>` 全量重建 |
| suspend_d 漏采 | D7 跨湖检测告警 | 人工跑 `sync_incremental.py --keys suspend_d` |
| daily 湖单日化（G6 活债） | **新增 D9：daily 湖行数 < 阈值告警** | 人工从 LFS 拉取 或 跑 `sync_data_lake.py --years N` 全量重建 |

### 6.5 与 `_fetch_with_guard` / `_recompute_symbol` 的整合

- **`_fetch_with_guard`（tushare_sync.py:71）**：已是所有 Tushare 调用的统一限频/熔断入口，repair_gaps 复用 `_fetch_paged`（内部调 pro，**未走 _fetch_with_guard**——repair_gaps.py:71 直接 `pro.daily()`，**这是个隐藏的限频裸奔点**！）。**整合动作**：repair_gaps 改为走 `_fetch_with_guard("daily", ...)` + `_fetch_with_guard("adj_factor", ...)`，与主管道同源限频。
- **`_recompute_symbol`（sync_daily_incremental.py:95）**：除权标的全历史 qfq 重算，已在日频增量内联。**整合动作**：抽出为 `data/integrity.py` 或 `data/tushare_sync.py` 的公共函数，让 D5 复权一致性检测能复用触发；C-9 memory 记录的「adj 空响应 KeyError 中断整批」已修（P1-A，adj 校验 sync_daily_incremental.py:126-129），整合时保留校验。

### 6.6 daily 湖双轨收口（G1）

两个选项（待毕业工单决策）：
- **选项 A（推荐）**：从 `TUSHARE_DATASETS` 删除 `"daily"` key（registry.py:707），daily 湖唯一写入口 = `sync_daily_incremental.py`。理由：daily 的前复权 + 除权检测 + 分页批量是独立物理路径，塞进通用 sync_dataset 框架不优雅；且生产已用 sync_daily_incremental。
- **选项 B**：反之，把 sync_daily_incremental 的逻辑合并进 tushare_sync（by=symbol + adj_api 已支持），删独立脚本。理由：配置驱动统一。但 daily 的按日分页（limit=500 绕 ConnectionReset）与 by=symbol 不兼容（registry 注释提过），合并成本高。
- **建议**：选项 A，风险低。

---

## 7. 待实证清单（反臆测）

| # | 待实证项 | 验证方法 | 影响 |
|---|----------|----------|------|
| E1 | **a_shares_daily.parquet 单日化真因**（G6）：LFS 指针未拉？测试覆盖？discovery 快照机制？ | `git lfs pull` 后重看行数；查 discovery 是否有"缩窗口快照"逻辑；查近期谁改了这文件 | 若是真数据债，是 P0；若 LFS，运维即可 |
| E2 | `TUSHARE_DATASETS["daily"]` 最近是否被 `sync_all_tushare --batch slow` 跑过（覆盖单日化？） | 查 `data_lake/.syncing/sync_all.log` + schtasks 历史 | 双轨冲突是否已发生 |
| E3 | repair_gaps 走 `_fetch_paged` 裸调 pro（6.5 节指出的限频裸奔）是否真的没走 `_fetch_with_guard` | 重读 repair_gaps.py:67-76 + _fetch_paged 定义（sync_daily_incremental.py:45-56，确实直接 `getattr(pro,api)(...)`） | 已确认，**属实** |
| E4 | `_backscan_recent` 是否真的从不自动触发 repair | 重读 sync_daily_incremental.py:264-271，确认只 warning | 已确认，**属实** |
| E5 | suspend_d 湖是否有自身漏采（D7 维度） | 跑 `scan_integrity` 但针对 suspend_d（当前 scan 只查 daily，不查 suspend_d） | 新维度需求 |
| E6 | `filter_universe_by_continuity` 的 fail-open 在生产是否曾经误放行（trade_days 空集场景） | 查 engine 日志 "完整性 gate 过滤" warning 频率；查 `_load_integrity_ctx` 失败率 | gate 实际有效性 |
| E7 | discovery 快照机制（commit message "discovery 新快照"）是否会主动缩 daily 湖窗口 | grep discovery 代码里写 a_shares_daily 的逻辑 | G6 真因排查 |
| E8 | weekly/monthly 湖（a_shares_weekly/monthly）是否有同样完整性缺口 | 跑 find_gaps 针对 weekly（当前只扫 daily） | 完整性维度扩展 |

---

## 8. 毕业建议（是否新建 data 重构工单）

**建议：毕业为 1 个 data 重构工单（可在 wayfinder MAP 立项），Question 草稿如下：**

> **data 层完整性根治实施（T5 毕业工单）**
>
> 基于 `research/T5-data-integrity.md` 调研，实施 data 层完整性根治：
>
> 1. **G6 活数据债排查（P0，先做）**：确认 `a_shares_daily.parquet` 单日化真因（LFS / 测试污染 / discovery 快照），恢复全历史湖。
> 2. **G2 scan/repair 自动化**：在 `ops/data_pipeline.py` T2 后追加步骤 ④（每日增量段 scan+auto repair）+ ⑤（每周全扫告警），消除手动 CLI 依赖。
> 3. **G3 生产 gate 维度扩展**：`run_data_check` 除 freshness 外，新增 D6 列完整性校验；T2 FAIL 重采链路接入 repair_gaps（不只调 sync_daily_incremental）。
> 4. **G1 daily 双轨收口**：从 `TUSHARE_DATASETS` 删除 `"daily"` key（选项 A），daily 湖唯一写入口 = sync_daily_incremental。
> 5. **6.5 限频裸奔修复**：repair_gaps 的 `_fetch_paged` 改走 `_fetch_with_guard`，与主管道同源限频/熔断。
> 6. **D5 复权一致性 + D7 跨湖对齐 + D9 行数阈值告警**：新增三个完整性维度，归 `data/integrity.py`。
> 7. **C3 follow-up**：repair_gaps 补采后对除权标的重算全历史 qfq 基准（消除补采段与历史段基线不一致）。
>
> 约束：TDD；不破坏现有 fail-open 语义（信号层 gate 保留作最后一道防线）；每步配额守卫。

---

## 附：信源索引

- 源码（本次实读）：`data/integrity.py`（全 296 行）/ `data/resilience.py`（全 306 行）/ `data/tushare_sync.py`（全 634 行）/ `data/lake_reader.py`（全 309 行）/ `data/fetcher.py`（AST 结构）/ `data/freshness.py`（全 88 行）/ `data/tools/sync_daily_incremental.py`（全 298 行）/ `data/tools/repair_gaps.py`（全 178 行）/ `data/tools/scan_integrity.py`（全 146 行）/ `data/tools/sync_incremental.py`（头 120 行）/ `data/tools/sync_all_tushare.py`（全 114 行）/ `data/tools/run_data_check.py`（全 178 行）/ `data/__init__.py`
- 源码（关键段）：`trading/engine.py:2640-2729`（_eod gate）/ `backtest/replay.py:85-165`（_apply_continuity_filter）/ `ops/data_pipeline.py:1-60`
- 配置：`config/data.py:1-119`（LAKE_CONFIG 全）/ `config/registry.py:48-49,114-115,184-187,413-431,707-734`（daily/suspend_d/weekly/monthly 注册）
- 实测：`data_lake/a_shares_daily.parquet`（3200 行单日）/ `data_lake/suspend_d.parquet`（13571 行 2023-07~2026-08）
- memory：`data-lake-integrity-gap`（11 天前）/ `akshare-jqdata-retired`（11 天前）/ `c9-data-observation-remediation-status`（3 天前）
- 文档：`docs/data-source-of-truth.md`（交易侧 SSoT，不覆盖 data 湖）/ `docs/data_pool.md`（**严重过时**，仍引 AKShare/JQData/dragon_list/sector/north_flow，全已退役——文档债）
- git：`data_lake/a_shares_daily.parquet` commit 历史（c242b5f3 等，LFS 入库）+ `.gitignore:85-88`（LFS 例外）
