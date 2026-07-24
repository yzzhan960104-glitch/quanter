# Tushare 数据快照扩容 · 设计文档（Spec）

- **日期**：2026-07-25
- **分支**：`feat/discovery-l0-l1`（承接，不新建）
- **作者**：Claude（自主执行模式，用户已 approve 方案 A 并授权全自主实施 + 启动）
- **状态**：已确认，进入实施

---

## 1. 背景与目标

用户要求"启动 Tushare 数据抓取"，按 **基础数据 500 次/分 + 特色数据 300 次/分** 两档配额，补齐 9 类数据集（股票/ETF/期权列表、低频行情日周月、沪港通、概念板块与成分、资金流向、筹码分布、量化因子），并本次就跑一轮全量回填。

项目已有成熟的"配置驱动"同步框架（`config/registry.py:TUSHARE_DATASETS` + `data/tushare_sync.py:sync_dataset`），但存在三处与目标不符：

1. **限频单桶 ~20/min**：`data/resilience.py:277` 的 `tushare_rate_limiter = RateLimiter(capacity=3, refill_rate=0.33)` 是为已废弃的 `tnskhdata` 代理调的旧参数（`data/_tushare_compat.py` 顶部注释确认 2026-07-24 已纯直连 Tushare 官方 SDK）。账户升级后官方配额为基础 500/min + 特色 300/min，旧限频器远低于配额上限，浪费吞吐。
2. **`quota_type` 仅日志标记**：`TUSHARE_DATASETS` 已有 `quota_type` 字段（仅 `cyq_perf` 用 `"special"`），但 `tushare_sync._sync_by_symbol:275` 只 `logger.debug` 标记，未真路由到独立限频桶。
3. **9 类数据集中 7 类缺失或过时**：见下表对照。

## 2. 需求决策摘要（6 轮澄清已确认）

| 决策点 | 结论 |
|---|---|
| 限频配额 | **按类别拆双桶**：基础 500/min、特色 300/min；`quota_type` 升级为真路由 |
| 完成定义 | **建能力 + 本次就跑全量回填**，盯到底报每集入库行数 |
| 代理 vs 直连 | 已纯直连 Tushare（代码确认），清理过时 `_unavailable` 注释，重探测代理坑数据集 |
| 个股行情管道 | **纳入注册表统一管道**；`scripts/` 后续废弃，能力收敛到 `data/` 层（高内聚低耦合） |
| 回填深度 | **近 5 年（2021-01-01 起）** |
| 复权口径 | **前复权 qfq**（与现有 `a_shares_daily.parquet` 一致：`raw × adj / adj_latest`） |
| 期权列表 opt_basic | **跳过**（YAGNI，项目当前无期权策略） |
| 筹码明细 cyq_chips | **保留**（用户要逐价位分布画筹码峰图） |

## 3. 范围

### 3.1 缺口对照表（用户清单 vs 注册表现状）

| 用户清单 | 现状 | 本次动作 | quota_type |
|---|---|---|---|
| 股票列表 stock_basic | ❌ 未落湖（`_load_universe` 内部用，不落 parquet） | **新增** `stock_basic` 数据集 | basic |
| ETF 列表 fund_basic | ✅ 已注册 | — | basic |
| ~~期权列表 opt_basic~~ | — | **跳过**（YAGNI） | — |
| 个股日线 daily | ⚠️ 走 `sync_data_lake.fetch_qfq` 独立管道，903 万行已落盘 | **迁移**到 `by="ohlcv_qfq"` 统一管道，保持公式一致 | basic |
| 个股周线 weekly | ❌ 全无 | **新增** `weekly`，`by="ohlcv_qfq"` | basic |
| 个股月线 monthly | ❌ 全无 | **新增** `monthly`，`by="ohlcv_qfq"` | basic |
| 沪港通股票列表 hs_const | ❌ 未注册 | **新增** `hs_const`（按 hs_type 分两湖） | basic |
| 概念板块列表 concept | ⚠️ `_unavailable`（过时，代理坑） | **清理** `_unavailable`，重探测直连可用性 | basic |
| 概念成分 concept_detail | ❌ 未注册（旧决策跳过） | **新增** `concept_detail`（按概念 id 分页） | basic |
| 资金流向 moneyflow | ✅ 已注册 | **改** `quota_type="special"`（特色数据归 300/min 桶） | special |
| 筹码胜率 cyq_perf | ✅ 已注册（`special`） | — | special |
| 筹码明细 cyq_chips | ❌ 未注册 | **新增** `cyq_chips`（逐价位分布，数据量大） | special |
| 量化因子 daily_basic | ❌ 未注册（fetcher 仅单标的实时拉） | **新增** `daily_basic`（每日全市场 PE/PB/换手率，by=date） | special |
| 量化因子 stk_factor_pro | ❌ 未注册 | **新增** `stk_factor_pro`（MACD/KDJ/BOLL 等技术因子，by=date） | special |

### 3.2 配额归属原则（按 Tushare 官方分类）

- **basic 桶（500/min）**：列表/行情类（stock_basic、hs_const、daily/weekly/monthly、fund_*、index_*、concept/concept_detail、宏观 cn_*/shibor/mkt_daily）
- **special 桶（300/min）**：特色/资金/筹码/因子类（moneyflow、cyq_perf、cyq_chips、daily_basic、stk_factor_pro、top_inst、margin_detail 等）

> 注：现有已注册数据集也需补标 `quota_type`（默认 `basic`，符合多数；`moneyflow`/`cyq_perf`/`top_inst`/`margin_detail`/`top10_*` 等归 `special`）。归类变更不影响已落湖数据，仅改限频桶。

## 4. 架构设计

### 4.1 双桶限频（`data/resilience.py`）

```python
# 基础桶：500/min（capacity=8 突发，refill_rate=8.3 token/s ≈ 498/min 稳态）
tushare_rate_limiter_basic = RateLimiter(name="tushare_basic", capacity=8, refill_rate=8.3)
# 特色桶：300/min（capacity=5 突发，refill_rate=5.0 token/s = 300/min 稳态）
tushare_rate_limiter_special = RateLimiter(name="tushare_special", capacity=5, refill_rate=5.0)

# 向后兼容别名：fetcher.py / sync_macro_credit.py 等 4 处旧调用零改
tushare_rate_limiter = tushare_rate_limiter_basic
```

**容量取值依据**：Tushare 官方限频是"每分钟滑动窗口"，令牌桶近似时 `refill_rate × 60 ≈ 配额` 即可；`capacity` 给少量突发（官方窗口边界允许短时突发）。保守留 0.5%~1% 余量，避免边界抖动触发 429。

**熔断器不变**：`tushare_breaker` 仍单实例（failure_threshold=3, recovery_timeout=60s），跨桶共享——熔断是"接口健康"语义，与限频桶正交。

### 4.2 quota_type 路由（`data/tushare_sync.py`）

`_fetch_with_guard` 签名扩展，按 `quota_type` 选桶：

```python
def _fetch_with_guard(api_name: str, *, quota_type: str = "basic", **kwargs) -> pd.DataFrame:
    limiter = tushare_rate_limiter_special if quota_type == "special" else tushare_rate_limiter_basic
    limiter.acquire(1.0)  # 替换原 tushare_rate_limiter.acquire
    # 其余熔断/退避逻辑不变
```

`sync_dataset` 从 `cfg["quota_type"]` 读出，下传给三个 `_sync_by_*`，再下传给 `_fetch_with_guard`。**改动链路单线**，零分支扩散。

### 4.3 注册表补齐（`config/registry.py`）

#### TUSHARE_DATASETS 新增 key（声明式配置，零新增分支代码）

```python
# —— 基础桶（500/min）——
"stock_basic": {
    # 股票列表（标的池源头）：单次拉全市场，by=single 落扁平 df。
    # list_status='L' 过滤在售（与 _load_universe 一致），不剔 ST（落湖保留全集，下游自行过滤）。
    "api": "stock_basic", "by": "single",
    "date_col": "list_date", "symbol_col": "ts_code",
    "fields": "ts_code,symbol,name,area,industry,market,list_date",
    "params": {"list_status": "L"},
    "lake": "data_lake/stock_basic.parquet",
    "quota_type": "basic",
},
"hs_const_sh": {  # 沪股通成分
    "api": "hs_const", "by": "single",
    "params": {"hs_type": "SH"},
    "date_col": "in_date", "symbol_col": "ts_code",
    "fields": "ts_code,hs_type,in_date,out_date,is_new",
    "lake": "data_lake/hs_const_sh.parquet",
    "quota_type": "basic",
},
"hs_const_sz": {  # 深股通成分（同上 hs_type=SZ）
    ...
},
"concept": {  # 清理 _unavailable，重探测直连可用
    "api": "concept", "by": "single",
    "date_col": "code", "symbol_col": "code",
    "fields": "code,name",
    "lake": "data_lake/concept.parquet",
    "quota_type": "basic",
    # 删除 _unavailable 字段（代理已废，直连重探测）
},
"concept_detail": {  # 概念成分股：按概念 id 分页
    # ⚠️ 特殊：需按 concept 列表的 id 逐个拉，by=symbol 复用（symbols=概念 id 列表）
    "api": "concept_detail", "by": "symbol",
    "universe": "concept",  # 新增 universe 类型，resolve_symbols 支持
    "code_param": "id",  # 传参名是 id 非 ts_code
    "date_col": "in_date", "symbol_col": "ts_code",
    "fields": "id,concept_name,ts_code,name,in_date,out_date",
    "lake": "data_lake/concept_detail.parquet",
    "quota_type": "basic",
},

# —— OHLCV 前复权（basic 桶）——
"daily": {
    "api": "daily", "by": "ohlcv_qfq",  # 新分页模式
    "adj_api": "adj_factor",  # 复权因子接口
    "freq": "D",  # D/W/M（pro.daily/pro.weekly/pro.monthly）
    "date_col": "trade_date", "symbol_col": "ts_code",
    "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
    "rename": {"vol": "volume"},
    "lake": "data_lake/a_shares_daily.parquet",  # 复用既有湖，保持一致性
    "quota_type": "basic",
},
"weekly":  { "api": "weekly",  "by": "ohlcv_qfq", "adj_api": "adj_factor", "freq": "W", ..., "lake": "data_lake/a_shares_weekly.parquet" },
"monthly": { "api": "monthly", "by": "ohlcv_qfq", "adj_api": "adj_factor", "freq": "M", ..., "lake": "data_lake/a_shares_monthly.parquet" },

# —— 特色桶（300/min）——
"cyq_chips": {  # 逐价位筹码分布（数据量极大：每日每标的 N 行）
    "api": "cyq_chips", "by": "date",
    "date_col": "trade_date", "symbol_col": "ts_code",
    "fields": "ts_code,trade_date,price,percent",
    "lake": "data_lake/cyq_chips.parquet",
    "quota_type": "special",
},
"daily_basic": {  # 每日全市场基本面因子（PE/PB/换手率/市值）
    "api": "daily_basic", "by": "date",
    "date_col": "trade_date", "symbol_col": "ts_code",
    "fields": "ts_code,trade_date,close,turnover_rate,pe,pe_ttm,pb,ps,total_mv,circ_mv",
    "lake": "data_lake/daily_basic.parquet",
    "quota_type": "special",
},
"stk_factor_pro": {  # 技术面因子（MACD/KDJ/BOLL/RSI...）
    "api": "stk_factor_pro", "by": "date",
    "date_col": "trade_date", "symbol_col": "ts_code",
    "fields": "ts_code,trade_date,close,macd,kdj_k,kdj_d,kdj_j,boll_upper,boll_mid,boll_lower,rsi_6,cci",
    "lake": "data_lake/stk_factor_pro.parquet",
    "quota_type": "special",
},
```

#### DATASET_REGISTRY 同步补元信息（前端 DataLakeView 反射）

每个新 TUSHARE_DATASETS key 都要在 `DATASET_REGISTRY` 加对应条目（source/market/granularity/script/freshness_hours）。`script` 统一标 `scripts/sync_tushare.py`（薄壳，server data_service 依赖）。

#### `resolve_symbols` 增强（`data/tushare_sync.py`）

新增 `universe="concept"` 分支：从 `data_lake/concept.parquet` 读概念 id 列表，供 `concept_detail` 的 `by=symbol`（code_param=id）消费。

### 4.4 OHLCV 前复权管道（`_sync_ohlcv_qfq`，新增到 `data/tushare_sync.py`）

**物理意图**：把 `sync_data_lake.fetch_qfq` + `sync_daily_incremental.sync_daily_incremental` 的前复权逻辑提炼为通用分支，支持 daily/weekly/monthly 三频。

**核心算法（与既有 `a_shares_daily.parquet` 字节级一致）**：
```
对每个交易日 td ∈ [start, end]（_trade_days 提供）:
    raw = pro.<api>(trade_date=td, limit=500, offset=0..N)   # 分页绕过 ConnectionReset
    adj = pro.adj_factor(trade_date=td, limit=500, offset=..)
    合并 raw + adj on (ts_code, trade_date)
每标的 latest_adj = groupby(ts_code).adj_factor.last()
price_qfq = raw_price × adj_factor / latest_adj
落 shard: data_lake/shards/<key>/<td>.parquet（by=date 分片）
_build_multiindex 合并 → MultiIndex(date, symbol)
```

**`sync_dataset` 路由**：
```python
elif by == "ohlcv_qfq":
    _sync_ohlcv_qfq(key, api, cfg["adj_api"], cfg["freq"], date_col, symbol_col,
                    start, end, resume, out, cfg=cfg)
```

**除权偏移标注**（沿用 `sync_daily_incremental:116-126`）：检测 adj 在 [start, end] 变化的标的，日志 warning 标注历史基准偏移（全量回填首次拉取无此问题，因 latest_adj 用 end 日为锚；增量追加时才有除权断崖）。

**weekly/monthly 复权锚点**：adj_factor 是日频，周/月线复权用该周/月**最后一个交易日**的 adj_factor 作 `latest_adj`（与 K 线收盘日对齐）。

### 4.5 统一 CLI 入口（`data/sync_cli.py`，新建）

```bash
python -m data.sync --all --since 2021-01-01          # 全量回填所有数据集
python -m data.sync --keys daily,weekly,cyq_chips --since 2021-01-01
python -m data.sync --keys moneyflow --incremental     # 增量（读湖最新日 d0 → d0+1..today）
python -m data.sync --keys daily --since 2021-01-01 --dry-run  # 小样例（1-2 日/标的）验证字段
python -m data.sync --quota basic                      # 仅跑基础桶数据集
```

**argparse**：`--keys`/`--all`/`--since YYYY-MM-DD`/`--end`（缺省今天）/`--incremental`/`--dry-run`/`--quota basic|special`/`--resume`（缺省 True）。

**main 流程**：
1. 解析 keys（`--all` = `TUSHARE_DATASETS.keys()`，可按 `--quota` 过滤）
2. 对每个 key 调 `sync_dataset(key, start, end, resume=True)`
3. 逐 key 打印入库行数/耗时/失败原因，汇总表结尾输出
4. 失败 key 不中断后续（fail-soft），结尾汇总 exit code（全成功 0，部分失败 1）

### 4.6 scripts/ 收敛策略（高内聚低耦合）

| 现有脚本 | 动作 |
|---|---|
| `scripts/sync_tushare.py` | **保留为薄壳**（server `data_service` 子进程依赖），内部转调 `data.sync_cli.main([key])` 或 `sync_dataset`。`DATASET_REGISTRY.script` 仍指向它。 |
| `scripts/sync_all_tushare.py` | **deprecated 薄壳**，转调 `python -m data.sync --all`，顶部加 DeprecationWarning。 |
| `scripts/sync_incremental.py` | **deprecated 薄壳**，转调 `python -m data.sync --incremental`。 |
| `scripts/sync_data_lake.py` | **deprecated**：`fetch_qfq` 逻辑迁移到 `_sync_ohlcv_qfq` 后，标 deprecated，转调 `python -m data.sync --keys daily`。 |
| `scripts/sync_daily_incremental.py` | **deprecated 薄壳**，转调 `python -m data.sync --keys daily --incremental`。 |

> **红线**：不删任何脚本（server/前端/schtasks 计划任务可能依赖），只转薄壳 + DeprecationWarning。真正删除留后续清理 PR。

## 5. 数据流

```
CLI (data.sync) / HTTP (POST /sync/{key}) / 旧 scripts/*.py 薄壳
                          │
                          ▼
              data/tushare_sync.sync_dataset(key, start, end)
                          │
            ┌─────────────┼─────────────┬──────────────┐
            ▼             ▼             ▼              ▼
      _sync_by_symbol _sync_by_date _sync_single _sync_ohlcv_qfq
            │             │             │              │
            └─────────────┴─────────────┴──────────────┘
                          ▼
            _fetch_with_guard(api, quota_type=cfg["quota_type"])
                          │
                ┌─────────┴─────────┐
                ▼                   ▼
   tushare_rate_limiter_basic   tushare_rate_limiter_special
        (500/min)                    (300/min)
                │                   │
                └─────────┬─────────┘
                          ▼
              pro.<api>(**kwargs)  +  tushare_breaker 熔断守卫
                          ▼
              shard 落盘 (断点续传) → _build_multiindex → data_lake/*.parquet
```

## 6. 错误处理

沿用 `_fetch_with_guard` 三态分类（已成熟，不改）：
- **transient**（限频/超时/断网）：指数退避 2/4/8/16/32s，重试不污染熔断计数
- **persistent**（积分/权限）：直接返空，不重试不熔断
- **unknown**：保守 record_failure 一次

**全量回填特有的容错**：
- 单 key 失败不中断后续 key（fail-soft，汇总报告）
- 断点续传（shard 已存在跳过）—— 重跑只补失败 key
- 配额耗尽（连续 transient 退避耗尽）：record_failure 走熔断，该 key 标失败，后续 key 不受影响（熔断是 per-key 维度？不，breaker 全局——需确认。**风险点见 §9**）

## 7. 测试策略（TDD）

严格 TDD：每个改动点先写测试再实现。测试文件沿用 `tests/test_tushare_*.py` 命名。

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_resilience_quota.py`（新） | 双桶独立计数、别名向后兼容、配额 refill_rate 校验 |
| `tests/test_tushare_sync_quota.py`（新） | `_fetch_with_guard` 按 quota_type 选桶、`sync_dataset` 下传 quota_type |
| `tests/test_tushare_datasets_snapshot.py`（新） | 新增 key 的 schema 完整性（api/by/date_col/fields/lake/quota_type 必填）、quota_type 归类正确性 |
| `tests/test_sync_ohlcv_qfq.py`（新） | 前复权公式 `raw × adj / latest`、weekly/monthly 锚点、与既有 `a_shares_daily.parquet` 抽样对比一致性 |
| `tests/test_sync_cli.py`（新） | argparse 参数解析、`--all`/`--keys`/`--quota` 过滤、fail-soft 汇总 |
| `tests/test_resolve_symbols_concept.py`（新） | `universe="concept"` 从 concept 湖读 id 列表 |

**dry-run 小样例验证**（全量回填前必做，保护配额）：
- 每个新 key 拉 1-2 日（by=date）/ 1-2 标的（by=symbol）/ 单次（by=single），验证 fields 真实性（防幻觉列）、落湖结构、行数合理。
- 用既有 `scripts/probe_tushare_fields.py`（项目已有探测习惯）对 cyq_chips/daily_basic/stk_factor_pro/concept_detail 做字段探测。

## 8. 全量回填计划

```bash
# 第 0 步：dry-run 小样例（每类 1-2 标的，保护配额）
python -m data.sync --keys stock_basic,hs_const_sh,concept,daily_basic,cyq_chips,stk_factor_pro \
    --since 2025-07-01 --end 2025-07-02 --dry-run

# 第 1 步：基础桶全量（500/min，预估 ~1.5h）
python -m data.sync --quota basic --since 2021-01-01

# 第 2 步：特色桶全量（300/min，预估 ~1h）
python -m data.sync --quota special --since 2021-01-01

# 第 3 步：OHLCV 三频（daily 迁移一致性验证后，再跑全量）
python -m data.sync --keys daily,weekly,monthly --since 2021-01-01
```

**回填后报告**：每 key 输出 `[key] rows=X symbols=Y elapsed=Zs status=OK/FAIL`，汇总到 `data_lake/.syncing/snapshot-2026-07-25.log`。

**daily 迁移一致性验证**（关键，保护 903 万行既有数据）：
1. 迁移前 `a_shares_daily.parquet` 备份
2. 新管道拉同一小区间（如 2025-06-01..2025-06-07）
3. 与旧管道 `sync_data_lake.fetch_qfq` 同区间产出做 `pd.testing.assert_frame_equal`
4. 一致后才允许新管道写 `a_shares_daily.parquet`

## 9. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| daily 迁移破坏 903 万行既有数据一致性 | 🔴 高 | §8 一致性验证（备份 + 小区间 assert_frame_equal）；新管道与旧 `fetch_qfq` 公式逐行对齐 |
| 全量回填烧配额（字段幻觉导致空列反复拉） | 🟡 中 | §7 dry-run 先探测字段真实性；断点续传重跑只补失败 key |
| 熔断器全局共享 → 单 key 限频耗尽 OPEN 拖累后续 | 🟡 中 | 现有 `_fetch_with_guard` 已有 breaker OPEN 冷却重试（1 次 recovery_timeout）；fail-soft 跳失败 key 不阻断后续 |
| cyq_chips 数据量爆炸（每日每标的 N 价位行） | 🟡 中 | by=date 单日全市场一次返，shard 按日分片；5 年 ≈ 1200 交易日 × 5000 标的 × ~10 价位 ≈ 6000 万行，落盘约 2-3GB（parquet 列压），可接受；dry-run 先验真实行数 |
| stk_factor_pro/daily_basic 字段名漂移 | 🟢 低 | dry-run 探测真实列名，fields 串按探测结果定，落湖前 _cleanse 兜底 |
| weekly/monthly 复权锚点错（adj_factor 日频 vs 周/月线） | 🟡 中 | 用该周/月最后交易日 adj_factor 作 latest_adj；dry-run 抽样验证周线 close ≈ 该周最后交易日 daily qfq close |
| concept_detail 按 id 分页可能 5000+ 概念 → 5000 请求 | 🟢 低 | basic 桶 500/min ≈ 10min，可接受；universe 走 concept 湖 id 列表 |

---

## 10. 不做的事（YAGNI）

- ❌ opt_basic 期权列表（项目无期权策略，用户确认跳过）
- ❌ pro_bar 黑盒复权（用手动 raw × adj / latest，更显式符合 Karpathy 哲学）
- ❌ Celery Beat 调度（项目决策点①=方案B，不引守护进程；schedule 仅元信息）
- ❌ 删除 scripts/*.py（只转薄壳 + DeprecationWarning，真正删除留后续）
- ❌ 重写 sync_dataset 为 OO 类（显式至上，函数式管道已够清晰）
