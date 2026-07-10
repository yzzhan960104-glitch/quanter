# data_lake 接入 + amount 单位统一 + min_rr_ratio 定标 设计

> 日期：2026-07-10
> 范围：蔡森形态学流水线 Phase 3+ 待办 ①/③/⑤（实盘上线阻断项）
> 前置：Phase 2 五个数学 bug 已修（merge 33690ca），安全整改已合并（acd3bb7）

## 目标

打通蔡森流水线的「真实数据 → 扫描 → 定标」闭环，消除三项实盘上线阻断：

1. **#1 data_lake 接入**：`caisen_service._load_price_data` 当前占位返空 → 接 `DataLakeReader`，让 scan/replay 能读真实全市场日线。
2. **#3 amount 单位统一**：data_lake `amount`=千元（tushare `pro.daily` 原生）vs `risk.liquidity_min_amount=1e8`(元)，量级差 1000× → 流动性过滤 0 命中。统一为元。
3. **#4 min_rr_ratio 定标**：Phase 2 Bug4 修后标准 W 底新公式 rr≈1.4 < 生产默认 3.0 → 发不出计划。用真实数据跑 replay 定标，把默认值改为数据驱动的合理值。

三项连锁：接入数据 → 统一单位 → 跑 replay 定标。

## 关键决策（已与用户确认）

| 决策点 | 选择 | 理由 |
|---|---|---|
| universe 范围 | **全市场**（~5000 标的） | 样本完整，符合全市场发现意图；性能在 scan 可接受、replay 用窗口控制 |
| amount 单位方向 | **装配时 ×1000 转元** | 数据入口一次性统一，risk 阈值（1亿=1e8）语义直观不动 |
| min_rr 产出 | **定标后改 config 默认** | 「能发出计划」立即生效，spec 记录数据依据 |
| 定标样本 | **近 3 年全市场** | 样本稳健，首次定标耗时几十分钟~几小时（一次性离线任务可接受） |

## 现状（探索结论）

- **真实数据已在**：`data_lake/a_shares_daily.parquet`（408MB，全市场 A 股，2016–2026，900 万行，tushare 前复权，MultiIndex(date,symbol)，列 open/high/low/close/volume/amount）。
- **amount 单位**：千元（tushare `pro.daily` 原生 `amount`）；volume：手。验证：amount mean=226052（≈2.26 亿/日均，千元口径合理）。
- **DataLakeReader**：多湖内存缓存，`load(path,key)` / `get_timeseries(symbol,start,end,lake)` / `get_cross_section(date,lake)`。`lakes["daily"]=a_shares_daily.parquet`，`default_lake="daily"`。lifespan 启动时已遍历 `LAKE_CONFIG["lakes"]` 全部 load。
- **_load_price_data**：占位返空 dict（注释明确"Phase 3 暂未接 lake"）。
- **replay**：`backtest_replay.replay` + `_recommend_min_rr` 就绪，`run_replay` 已串接，输出含 `min_rr_ratio_recommendation`。

## 架构与数据流

### 改造核心：`caisen_service._load_price_data(symbols, date)`

```
_load_price_data(symbols, date):
    reader = DataLakeReader.get_instance()
    if not reader.loaded: return {}           # 离线降级（既有契约不变）
    lake = LAKE_CONFIG["default_lake"]        # "daily"
    # universe 解析：None/空 → 全市场枚举
    if not symbols: symbols = reader.symbols(lake)
    price_data = {}
    for sym in symbols:
        # start 用足够早的固定日期（早于 daily 湖起点 2016），get_timeseries 的
        # .loc[start:end] 闭区间切片会自然截到实际数据范围；end=date 截到 T 日（无前视）。
        ts = reader.get_timeseries(sym, start="2010-01-01", end=date, lake=lake)
        if ts.empty: continue
        ts = ts[["open","high","low","close","volume","amount"]].copy()
        ts["amount"] = ts["amount"] * 1000.0   # #3 千元→元（流动性过滤口径统一）
        price_data[sym] = ts
    return price_data
```

- **scan**：`run_scan` 传 `date=T`，`_load_price_data` 取每个 symbol `[:T]`（严格无前视）。
- **replay**：`run_replay` 用 `req.start`~`req.end` 作 `get_timeseries` 的 start/end，`backtest_replay.replay` 内部按 T 滚动 `.loc[:T]`。

### 配套：DataLakeReader 新增 `symbols(lake=None)` 公开方法

封装全市场枚举，避免 `_load_price_data` 穿透私有 `_lakes`：
```
def symbols(self, lake=None) -> list[str]:
    key = self._resolve(lake)
    if key is None or key not in self._lakes: return []
    return list(self._lakes[key].index.get_level_values("symbol").unique())
```

### 性能策略（全市场关键挑战）

| 场景 | 量级 | 策略 | 预期 |
|---|---|---|---|
| scan（每日盘后 beat） | ~5000 标的 × 单次 screener `.loc[:T]` | reader 已在内存、xs 快；screener 串行 | 分钟级，可接受 |
| replay 定标（一次性离线） | 近 3 年全市场滚动 | `scripts/calibrate_min_rr.py` 默认近 3 年，`--sample N` 可采样、`--years N` 可调窗口 | 几十分钟~几小时（一次性） |

scan 的性能若后续不达标，再考虑并行/向量化优化（YAGNI，先跑通）。

## #4 定标交付

新增 `scripts/calibrate_min_rr.py`：
- 默认参数：近 3 年、全市场（`--sample`/`--years` 可调）。
- 流程：`_load_price_data(全市场, start)` → `backtest_replay.replay` → 打印 ReplayReport（n_hits/win_rate/avg_rr/max_drawdown/min_rr_ratio_recommendation）+ rr 分布直方图。
- 产出：`_recommend_min_rr` 的建议值 + 数据依据（n_hits/胜率/avg_rr）。
- 落地：据建议值改 `caisen/config.py` 的 `min_rr_ratio` 默认（3.0 → 建议值），数据依据记入 commit message + config 字段 description。

## 错误处理

- reader 未 load daily（离线/CI）→ `_load_price_data` 返空 → `run_scan` 返 `[]`、`run_replay` 返零统计（既有降级契约不变）。
- 单标的 `get_timeseries` 空/异常 → 跳过该标的（screener 既有容错）。
- 全市场枚举为大列表 → `_load_price_data` 逐标的装配，内存只保留当次扫描所需（reader 持全量、service 持子集）。
- `amount ×1000` 仅在装配副本上做（`ts.copy()`），不改 reader 缓存（避免污染其他消费方）。

## 测试策略

- `_load_price_data` 接入：用 monkeypatch 构造一个假 reader（小样本 MultiIndex df），验证 ①返回 `{symbol:df}` 非空 ②`universe=None` 时枚举全部 symbol ③`amount` 装配后 = 原值×1000 ④reader 未 load 时返空。
- `DataLakeReader.symbols()`：小样本 df 验证返回唯一 symbol 列表。
- `calibrate_min_rr.py`：小样本（`--sample 5 --years 1`）跑通，产出非空建议值。
- 既有测试（test_caisen_service/api）用 monkeypatch 注入合成 price_data，不受 reader 接入影响（仍走注入路径）。

## 验收标准

1. `_load_price_data` 接 reader 后，`run_scan` 在真实 daily 湖上能产出非空候选（流动性过滤有命中，单位正确）。
2. `amount` 装配后为元量级，`liquidity_filter` 不再 0 命中。
3. `calibrate_min_rr.py` 跑近 3 年全市场产出 min_rr 建议值，config 默认改为该值后，标准 W 底计划能通过 rr 过滤（scan 有计划输出）。
4. 全量 pytest 绿，既有测试不回归。

## 不在本次范围（YAGNI）

- scan 全市场性能优化（并行/向量化）：先跑通，不达标再优化。
- DATASET_REGISTRY 把 daily 的 source 标注从 AKShare 改 Tushare（标注 bug，独立小项，不顺带）。
- daily_active 活跃池湖的接入（用户选全市场，活跃池非本次）。
- 跨进程 reader load（celery worker）的深度优化——worker 侧 ensure-load 在实现时处理，不单列设计。
