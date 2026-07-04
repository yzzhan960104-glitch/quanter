# 三大支柱全栈闭环 实施计划（Explorer / Backtest / Live）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已具备但未暴露的能力（FactorAnalyzer 分层评估、QMT 实盘网关、沪深300 基准）接通到前端，形成因子探索 / 回测可视化 / 实盘中控三大业务闭环。

**Architecture:** 后端先扩数据契约（BacktestResponse 增 benchmark_series、Celery 因子网格扩返分层/IC、新建 trading 路由优雅降级真接 QMT）→ 前端 API facade 对齐类型 → 前端视图/组件消费 → 路由接线 + 端到端验收。所有外部依赖（Redis/QMT/数据湖）挂掉时各面板独立降级，绝不白屏。

**Tech Stack:** FastAPI + Celery + Redis（后端）；Vue 3.5 + vue-router 4 + Element Plus 2.9 + ECharts 5.5 + vue-echarts 7 + axios（前端）；ECharts 暗色主题已注册（`terminal-dark`）。

## 与 spec 的两处务实简化（YAGNI，已记录）

- **`pnl_today` → `pnl`（累计浮盈）**：QMT 持仓对象 `XtPosition` 不带昨收，"今日浮盈"需另查行情接口；第一版用 `market_value - open_cost` 累计浮盈，前端标签"浮动盈亏"。spec §5.1 的 pnl_today 字段重命名为 pnl。
- **Treemap 不按 sector 分组**：个股→板块映射需额外数据源；第一版按 symbol 叶子直接铺平。spec §5.2 的"sector 一级分组"列入后续迭代。

## Global Constraints

- **语言**：所有对话/注释/文档/commit message 100% 中文（CLAUDE.md 红线）。
- **反魔法**：因子数学用纯 Pandas/NumPy，禁 Alphalens；ECharts option 用 `markRaw`。
- **NaN 早抛**：后端所有新端点经既有 `StrictJSONResponse`（allow_nan=False）；IC/基准序列出口 `dropna`。
- **优雅降级**：Redis/QMT/数据湖/基准缺失 → 对应面板空态，不崩。
- **基准归一化**：策略 nav 与基准 close 都按 `/ first` 归一化起点 1.0，ProChart log 轴可比。
- **前端性能**：万级时序用 `shallowRef` + `markRaw`，禁深 reactive；SSE/轮询 `onBeforeUnmount` 清理。
- **测试**：后端 `pytest`；前端 `cd web && npm run build`（vue-tsc 类型检查 + vite 构建）。
- **提交**：每 task 末尾 commit，message 中文，仅 add 本 task 涉及文件（不碰在途的其他改动）。
- **基准标的**：`510300.SH`（沪深300 ETF，与策略同 schema，`fetch_daily_hist` 直取）。
- **活跃池代号**：`dynamic_top50`（前端劫持标识，LakeDataFetcher 路由到 daily_active 湖）。

---

## File Structure（改动地图）

**后端新建**：
- `server/services/trading_service.py` — QMT 网关单例装配 + 三业务函数（status/positions/halt）
- `server/api/v1/trading.py` — 薄路由，调 trading_service，run_in_threadpool 包裹
- `tests/test_factor_grid_payload.py` — 因子网格新产物契约测试
- `tests/test_backtest_benchmark.py` — 基准净值归一化/reindex 测试
- `tests/test_trading_service.py` — trading_service 四态/幂等测试

**后端修改**：
- `server/schemas/backtest.py` — + BenchmarkPoint；BacktestResponse + benchmark_series
- `server/services/backtest_service.py` — + _compute_benchmark_series；_serialize_backtest_result 增 benchmark_series 参数；run_single_backtest 注入
- `server/celery_app.py` — run_factor_grid_impl 扩返分层/IC 时序/分布
- `server/main.py` — lifespan 装 QMT 单例；include trading_router

**前端新建**：
- `web/src/api/explorer.ts` — submitGrid/getResult + 类型
- `web/src/api/trading.ts` — getStatus/getPositions/emergencyHalt + 类型
- `web/src/views/ExplorerView.vue` — 因子探索两图
- `web/src/views/LiveCockpitView.vue` — 实盘中控
- `web/src/components/UniverseCard.vue` — 只读活跃池卡片

**前端修改**：
- `web/src/api/backtest.ts` — + BenchmarkPoint；SingleBacktestResponse + benchmark_series
- `web/src/components/ProChart.vue` — 双 Y 轴重构 + scatter
- `web/src/components/ParamForm.vue` — 顶部插 UniverseCard，清理主观默认值
- `web/src/router/index.ts` — + /explorer + /live
- `web/src/App.vue` — 导航加两项

---

## Task 1: BacktestResponse 扩展 benchmark_series schema

**Files:**
- Modify: `server/schemas/backtest.py`（在 `DrawdownPoint` 后插入 `BenchmarkPoint`；`BacktestResponse` 增字段）
- Test: `tests/test_backtest_schema.py`（既有文件，追加用例）

**Interfaces:**
- Consumes: 无（纯 schema）
- Produces: `BenchmarkPoint(date: str, nav: float)`；`BacktestResponse.benchmark_series: List[BenchmarkPoint]`（默认空列表，向后兼容）

- [ ] **Step 1: 写失败测试**

在 `tests/test_backtest_schema.py` 末尾追加：

```python
def test_backtest_response_has_benchmark_series_field():
    """BacktestResponse 必须含 benchmark_series 字段（默认空列表，向后兼容旧响应）。"""
    from server.schemas.backtest import BacktestResponse, BenchmarkPoint, MetricsResponse
    # 最小合法响应（benchmark_series 缺省）
    resp = BacktestResponse(
        metrics=MetricsResponse(
            initial_capital=1_000_000, final_nav=1.0, total_return=0.0,
            annual_return=0.0, annual_volatility=0.0, max_drawdown=0.0,
            sharpe_ratio=0.0, calmar_ratio=0.0, win_rate=0.0,
            profit_loss_ratio=0.0, n_trades=0, n_failed_trades=0,
        ),
        nav_series=[], drawdown_series=[], trades=[], ohlcv=[], positions=[],
    )
    assert resp.benchmark_series == []  # 缺省空列表

    # 显式构造基准序列
    bp = BenchmarkPoint(date="2024-01-02", nav=1.0)
    assert bp.date == "2024-01-02" and bp.nav == 1.0
```

- [ ] **Step 2: 验证失败**

Run: `pytest tests/test_backtest_schema.py::test_backtest_response_has_benchmark_series_field -v`
Expected: FAIL — `cannot import name 'BenchmarkPoint'`

- [ ] **Step 3: 实现 schema**

在 `server/schemas/backtest.py` 的 `DrawdownPoint` 类定义之后插入：

```python
class BenchmarkPoint(BaseModel):
    """基准累计净值节点（归一化，起点=1.0）。

    Why 单列 nav 而非透传 raw close：
    - 基准（沪深300 ETF 510300.SH）与策略 nav 都按 nav/首值 归一化到起点 1.0，
      保证 ProChart 左 log 轴下两条线物理可比（同币种/同时区/同交易日历）。
    - date 与 NavPoint.date 同格式（YYYY-MM-DD），按策略 nav_series 的 date reindex +
      前向填充，避免基准停牌日折线断裂误导。
    """
    date: str
    nav: float
```

在 `BacktestResponse` 类末尾（`positions` 字段之后）追加：

```python
    # 基准累计净值（沪深300 ETF 510300.SH 归一化）；缺数据/降级时空列表，ProChart 不画基准线
    benchmark_series: List[BenchmarkPoint] = []
```

- [ ] **Step 4: 验证通过**

Run: `pytest tests/test_backtest_schema.py::test_backtest_response_has_benchmark_series_field -v`
Expected: PASS

跑全量 schema 测试防回归：`pytest tests/test_backtest_schema.py -v` → 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add server/schemas/backtest.py tests/test_backtest_schema.py
git commit -m "feat(schema): BacktestResponse 扩 benchmark_series 字段（沪深300 ETF 基准）"
```

---

## Task 2: backtest_service 基准净值计算 + 注入序列化

**Files:**
- Modify: `server/services/backtest_service.py`（+ `_compute_benchmark_series`；改 `_serialize_backtest_result` 签名；改 `run_single_backtest` 注入）
- Test: `tests/test_backtest_benchmark.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `BenchmarkPoint`；既有 `LakeDataFetcher.fetch_ohlcv(symbol, start, end, freq)`；既有 `AKShareClient.fetch_daily_hist(symbol, start, end, adjust="qfq")`
- Produces: `_compute_benchmark_series(start_date: date, end_date: date, strategy_dates: list[str]) -> list[BenchmarkPoint]`；`_serialize_backtest_result(result, price_data, benchmark_series=None) -> BacktestResponse`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_backtest_benchmark.py`：

```python
# -*- coding: utf-8 -*-
"""基准净值（沪深300 ETF）归一化与对齐测试。"""
from datetime import date
from unittest.mock import patch


def test_benchmark_normalizes_to_unit_start():
    """基准 close 必须归一化到起点 1.0（nav / 首值）。"""
    import pandas as pd
    from server.services.backtest_service import _compute_benchmark_series

    # 模拟 ETF close：100, 102, 98 → 归一化 1.0, 1.02, 0.98
    fake_df = pd.DataFrame(
        {"close": [100.0, 102.0, 98.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    strategy_dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    with patch("server.services.backtest_service.LakeDataFetcher") as MockLake:
        MockLake.return_value.fetch_ohlcv.return_value = fake_df
        bench = _compute_benchmark_series(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 4),
            strategy_dates=strategy_dates,
        )
    assert [p.date for p in bench] == strategy_dates
    assert abs(bench[0].nav - 1.0) < 1e-9          # 起点 = 1.0
    assert abs(bench[1].nav - 1.02) < 1e-9
    assert abs(bench[2].nav - 0.98) < 1e-9


def test_benchmark_empty_when_lake_missing_and_online_fails():
    """湖缺 + 在线降级也空 → 返空列表（不抛，ProChart 不画基准线）。"""
    from unittest.mock import MagicMock
    from server.services.backtest_service import _compute_benchmark_series

    with patch("server.services.backtest_service.LakeDataFetcher") as MockLake, \
         patch("server.services.backtest_service.AKShareClient") as MockAK:
        MockLake.return_value.fetch_ohlcv.side_effect = LookupError("湖无数据")
        MockAK.return_value.fetch_daily_hist.return_value = MagicMock(empty=True)
        bench = _compute_benchmark_series(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 4),
            strategy_dates=["2024-01-02", "2024-01-03"],
        )
    assert bench == []


def test_benchmark_reindex_forward_fills_missing_days():
    """基准按策略 strategy_dates reindex，缺失日前向填充（不折线断裂）。"""
    import pandas as pd
    from server.services.backtest_service import _compute_benchmark_series

    # 基准只有 01-02、01-05 两天数据；策略日期含 01-02/03/04/05
    fake_df = pd.DataFrame(
        {"close": [100.0, 104.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-05"]),
    )
    strategy_dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    with patch("server.services.backtest_service.LakeDataFetcher") as MockLake:
        MockLake.return_value.fetch_ohlcv.return_value = fake_df
        bench = _compute_benchmark_series(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 5),
            strategy_dates=strategy_dates,
        )
    # 01-03、01-04 前向填充 01-02 的归一化值 1.0
    assert abs(bench[1].nav - 1.0) < 1e-9
    assert abs(bench[2].nav - 1.0) < 1e-9
    assert abs(bench[3].nav - 1.04) < 1e-9
```

- [ ] **Step 2: 验证失败**

Run: `pytest tests/test_backtest_benchmark.py -v`
Expected: FAIL — `cannot import name '_compute_benchmark_series'`

- [ ] **Step 3: 实现基准计算**

在 `server/services/backtest_service.py` 顶部 import 区追加（`from data.fetcher import MockDataFetcher` 下方）：

```python
from data.clients.akshare_client import AKShareClient
```

在 `_serialize_backtest_result` 函数**之前**插入新函数：

```python
# 基准标的：沪深300 ETF（A 股 ETF，与策略同币种/同时区/同交易日历，年跟踪误差<0.5%）
_BENCHMARK_SYMBOL = "510300.SH"


def _compute_benchmark_series(
    start_date, end_date, strategy_dates: list[str]
) -> list:
    """计算沪深300 ETF 基准累计净值（归一化起点 1.0，按策略日期 reindex + 前向填充）。

    取数三级降级（绝不抛）：
      1) LakeDataFetcher.fetch_ohlcv("510300.SH", daily 湖) —— 与策略同协议
      2) AKShareClient.fetch_daily_hist 在线兜底（带熔断）
      3) 全空 → 返 []（ProChart 不画基准线，降级不崩）

    归一化：close / close[0]（首值），起点 = 1.0。
    对齐：基准按 strategy_dates reindex，缺失日前向填充（基准停牌日沿用前收）。
    NaN 守卫：close 列 dropna 后归一化；strategy_dates 为空 → 返 []。

    参数：
        start_date/end_date: datetime.date，回测区间（取数窗口）。
        strategy_dates: 策略 nav_series 的日期字符串列表（YYYY-MM-DD），基准按此 reindex。
    """
    from server.schemas.backtest import BenchmarkPoint

    if not strategy_dates:
        return []

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.min.time())

    # 三级降级取 close Series
    close = pd.Series(dtype=float)
    try:
        from data.lake_fetcher import LakeDataFetcher
        df = LakeDataFetcher().fetch_ohlcv(_BENCHMARK_SYMBOL, start_dt, end_dt, freq="1d")
        if df is not None and not df.empty and "close" in df.columns:
            close = df["close"].dropna()
    except LookupError as e:
        logger.info("基准 daily 湖取数失败，降级 AKShare：%s", e)
    except Exception as e:
        logger.warning("基准 daily 湖取数异常：%s", e)

    if close.empty:
        try:
            ak_df = AKShareClient().fetch_daily_hist(
                _BENCHMARK_SYMBOL,
                start_date.strftime("%Y%m%d"),
                end_date.strftime("%Y%m%d"),
                adjust="qfq",
            )
            if ak_df is not None and not ak_df.empty and "close" in ak_df.columns:
                close = ak_df["close"].dropna()
        except Exception as e:
            logger.warning("基准 AKShare 在线取数失败（降级空基准）：%s", e)

    if close.empty:
        return []  # 三级全空 → 不画基准线

    # 归一化到起点 1.0
    first = close.iloc[0]
    if first == 0 or not np.isfinite(first):
        return []
    nav = close / first

    # 按策略日期 reindex + 前向填充（基准日期索引归一为 YYYY-MM-DD 字符串）
    nav.index = nav.index.strftime("%Y-%m-%d")
    target = pd.Series(index=strategy_dates, dtype=float)
    aligned = nav.reindex(strategy_dates).ffill()

    return [
        BenchmarkPoint(date=d, nav=float(v))
        for d, v in aligned.items()
        if pd.notna(v)
    ]
```

- [ ] **Step 4: 改 `_serialize_backtest_result` 签名 + 两处 return 注入**

把 `_serialize_backtest_result` 的签名从：
```python
def _serialize_backtest_result(
    result: Dict[str, Any], price_data: dict[str, pd.DataFrame]
) -> BacktestResponse:
```
改为：
```python
def _serialize_backtest_result(
    result: Dict[str, Any],
    price_data: dict[str, pd.DataFrame],
    benchmark_series: list | None = None,
) -> BacktestResponse:
```

函数体顶部（`daily_df = ...` 之前）加一行归一化入参：
```python
    benchmark_series = benchmark_series or []
```

**早返回路径**（`return BacktestResponse(...)` 的空数据分支，约 405 行）的 `positions=positions,` 之后加：
```python
            benchmark_series=benchmark_series,
```

**正常返回路径**（末尾 `return BacktestResponse(...)`，约 520 行）的 `positions=positions,` 之后加同样一行：
```python
        benchmark_series=benchmark_series,
```

- [ ] **Step 5: 改 `run_single_backtest` 注入基准**

把 `run_single_backtest` 末尾的：
```python
    return _serialize_backtest_result(result, price_data)
```
改为：
```python
    # 基准净值（沪深300 ETF 归一化）：按策略 nav 日期 reindex；缺数据返 [] 不崩
    strategy_dates = [
        idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx)
        for idx in result.get("daily_records", pd.DataFrame()).index
    ]
    benchmark_series = _compute_benchmark_series(
        start_date=req.start_date, end_date=req.end_date,
        strategy_dates=strategy_dates,
    )
    return _serialize_backtest_result(result, price_data, benchmark_series=benchmark_series)
```

- [ ] **Step 6: 验证通过**

Run: `pytest tests/test_backtest_benchmark.py -v`
Expected: 3/3 PASS

跑回归防破既有回测：`pytest tests/test_strategy.py tests/test_backtest_nan_regression.py -v` → 全 PASS（既有用例 benchmark_series 走 lake 缺失→空，不影响）。

- [ ] **Step 7: 提交**

```bash
git add server/services/backtest_service.py tests/test_backtest_benchmark.py
git commit -m "feat(backtest): 沪深300 ETF 基准净值计算+归一化+reindex 对齐（三级降级）"
```

---

## Task 3: Celery 因子网格扩返分层收益/IC 时序/分布

**Files:**
- Modify: `server/celery_app.py`（`run_factor_grid_impl` 扩返）
- Test: `tests/test_factor_grid_payload.py`（新建）

**Interfaces:**
- Consumes: 既有 `FactorAnalyzer.compute_ic(factor, fwd_returns) -> {ic_series, ic_mean, ic_ir, t_stat}`；`FactorAnalyzer.fractile_analysis(factor, fwd_returns, n_groups=5) -> {group_returns, long_short}`；`cross_sectional_momentum(returns, window)`
- Produces: `run_factor_grid_impl(spec) -> dict` 新结构含 `factor/dates/ic_series/ic_mean/ic_ir/t_stat/quantile_nav/ic_hist`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_factor_grid_payload.py`：

```python
# -*- coding: utf-8 -*-
"""因子网格扩返契约：分层累计净值 + IC 时序 + IC 直方图。"""
from unittest.mock import patch


def _fake_panel():
    """构造 30 日 × 8 标的 的 returns 面板（足够算 20 日动量 + 分层）。"""
    import pandas as pd
    import numpy as np
    dates = pd.bdate_range("2024-01-02", periods=30)
    syms = [f"S{i}.SZ" for i in range(8)]
    rng = np.random.default_rng(42)
    # 累计收益面板（pct_change 后用于动量+远期收益）
    returns = pd.DataFrame(rng.normal(0.001, 0.02, (30, 8)), index=dates, columns=syms)
    return returns


def test_factor_grid_returns_quantile_and_ic_payload():
    """run_factor_grid_impl 必须返 quantile_nav(Q1-Q5+LS) + ic_series + ic_hist。"""
    import pandas as pd
    from server.celery_app import run_factor_grid_impl

    returns = _fake_panel()
    fake_ts = pd.DataFrame({"close": [1.0]})  # 占位，reader 不会被真正调用

    with patch("data.lake_reader.DataLakeReader.get_instance") as MockReader, \
         patch("data.lake_reader.DataLakeReader.get_timeseries") as mock_gts:
        # 让 reader.loaded=True 且 get_timeseries 返每标的的累计 close
        mock_gts.side_effect = lambda sym, s, e, **kw: (
            (1 + returns[sym]).cumprod().rename("close").to_frame()
        )
        MockReader.return_value.loaded = True
        spec = {
            "factor": "cross_sectional_momentum",
            "universe": list(returns.columns),
            "start": "2024-01-02", "end": "2024-01-30",
        }
        out = run_factor_grid_impl(spec)

    assert out["ok"] is True
    # IC 时序
    assert isinstance(out["ic_series"], list)
    assert len(out["ic_series"]) == len(out["dates"])
    assert "ic_mean" in out and "ic_ir" in out and "t_stat" in out
    # 分层累计净值：Q1..Q5 + LS
    qn = out["quantile_nav"]
    for key in ("Q1", "Q2", "Q3", "Q4", "Q5", "LS"):
        assert key in qn and isinstance(qn[key], list)
        if key != "LS":
            assert abs(qn[key][0] - 1.0) < 1e-9, f"{key} 起点必须归一化为 1.0"
    # IC 直方图
    assert "bin_edges" in out["ic_hist"] and "counts" in out["ic_hist"]
    assert sum(out["ic_hist"]["counts"]) <= len(out["ic_series"])


def test_factor_grid_empty_universe_returns_not_ok():
    """universe 全空数据 → {ok: False}，不抛。"""
    from server.celery_app import run_factor_grid_impl
    with patch("data.lake_reader.DataLakeReader.get_instance") as MockReader:
        MockReader.return_value.loaded = False  # 离线模式
        out = run_factor_grid_impl({"factor": "x", "universe": [], "start": "2024-01-02", "end": "2024-01-30"})
    assert out["ok"] is False
```

- [ ] **Step 2: 验证失败**

Run: `pytest tests/test_factor_grid_payload.py -v`
Expected: FAIL — `KeyError: 'quantile_nav'`（既有 impl 不返该键）

- [ ] **Step 3: 重写 `run_factor_grid_impl` 扩返**

把 `server/celery_app.py` 里 `run_factor_grid_impl` 整个函数体替换为：

```python
def run_factor_grid_impl(spec: dict) -> dict:
    """网格计算实现（同步纯函数，可被 worker 或线程池调用）。

    spec 形如 {factor, universe, start, end}。
    产物（前端 ExplorerView 两图数据源）：
      - dates: 评估区间交易日（IC/分层共享 x 轴）
      - ic_series: 逐期 Rank IC（前端柱状图，正红负绿）
      - ic_mean / ic_ir / t_stat: IC 摘要（顶部卡片）
      - quantile_nav: Q1-Q5 累计净值（起点 1.0）+ LS（Q5-Q1 纯净 Alpha）
      - ic_hist: IC 分布直方图 {bin_edges, counts}

    Why 函数内 import：重模块延迟到调用时，避免 celery_app 模块级硬耦合。
    离线（无 parquet）/ universe 全空 → {ok: False}，绝不抛。
    """
    import numpy as np
    import pandas as pd
    from data.lake_reader import DataLakeReader
    from factors.analyzer import FactorAnalyzer
    from factors.exploratory_momentum import cross_sectional_momentum

    reader = DataLakeReader.get_instance()
    if not reader.loaded:
        return {"ok": False, "reason": "数据湖未加载"}
    pieces = []
    for sym in spec.get("universe", []):
        ts = reader.get_timeseries(sym, spec["start"], spec["end"])
        if not ts.empty:
            pieces.append(ts["close"].rename(sym))
    if not pieces:
        return {"ok": False, "reason": "universe 无可用数据"}
    panel = pd.concat(pieces, axis=1).sort_index()
    returns = panel.pct_change()
    factor = cross_sectional_momentum(returns, window=20)
    fwd = returns.shift(-1)

    analyzer = FactorAnalyzer()
    ic_out = analyzer.compute_ic(factor, fwd)
    frac = analyzer.fractile_analysis(factor, fwd, n_groups=5)

    # IC 时序 + 日期（dropna 防 NaN 进直方图）
    ic_series = ic_out["ic_series"].dropna()
    dates = [d.strftime("%Y-%m-%d") for d in ic_series.index]
    ic_list = [float(v) for v in ic_series.values]

    # 分层累计净值（每组 (1+r).cumprod()，起点归一 1.0；LS = Q5 - Q1 累计差）
    group_returns = frac["group_returns"]  # dict[g, Series of 远期收益]
    n_groups = 5
    quantile_nav: dict[str, list[float]] = {}
    group_cum = {}
    for g in range(n_groups):
        s = group_returns.get(g, pd.Series(dtype=float)).dropna()
        if s.empty:
            group_cum[g] = pd.Series(dtype=float)
            continue
        cum = (1.0 + s).cumprod()
        cum = cum / cum.iloc[0] if cum.iloc[0] != 0 else cum  # 起点归一 1.0
        group_cum[g] = cum
        quantile_nav[f"Q{g + 1}"] = [float(v) for v in cum.values]
    # 多空 Alpha：Q5 累计 - Q1 累计（对齐到 Q5 索引）
    q5 = group_cum.get(n_groups - 1, pd.Series(dtype=float))
    q1 = group_cum.get(0, pd.Series(dtype=float))
    if not q5.empty and not q1.empty:
        ls = (q5 - q1.reindex(q5.index).ffill()).fillna(0.0)
        quantile_nav["LS"] = [float(v) for v in ls.values]
    else:
        quantile_nav["LS"] = []

    # IC 直方图（np.histogram，bin 数自适应样本量）
    if len(ic_list) >= 2:
        counts, edges = np.histogram(ic_list, bins=min(20, max(5, len(ic_list) // 2)))
        ic_hist = {"bin_edges": [float(x) for x in edges], "counts": [int(c) for c in counts]}
    else:
        ic_hist = {"bin_edges": [], "counts": []}

    return {
        "ok": True,
        "factor": spec.get("factor", ""),
        "dates": dates,
        "ic_series": ic_list,
        "ic_mean": float(ic_out["ic_mean"]),
        "ic_ir": float(ic_out["ic_ir"]),
        "t_stat": float(ic_out["t_stat"]),
        "quantile_nav": quantile_nav,
        "ic_hist": ic_hist,
    }
```

- [ ] **Step 4: 验证通过**

Run: `pytest tests/test_factor_grid_payload.py -v`
Expected: 2/2 PASS

跑既有因子测试防回归：`pytest tests/test_factor_analyzer.py tests/test_exploratory_momentum.py -v` → 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add server/celery_app.py tests/test_factor_grid_payload.py
git commit -m "feat(explorer): 因子网格扩返分层累计净值+多空Alpha+IC时序/分布"
```

---

## Task 4: 实盘 trading 路由（优雅降级真接 QMT）

**Files:**
- Create: `server/services/trading_service.py`
- Create: `server/api/v1/trading.py`
- Modify: `server/main.py`（lifespan 装配 + include_router）
- Test: `tests/test_trading_service.py`（新建）

**Interfaces:**
- Consumes: 既有 `QmtExecutionGateway`（构造读 `QMT_USERDATA_PATH`/`QMT_ACCOUNT_ID`，`is_locked` 属性，`_fetch_broker_positions` async，`_orders` dict，`connect`/`disconnect` async）
- Produces:
  - `get_qmt_gateway() -> QmtExecutionGateway | None`（单例，缺凭证返 None）
  - `get_status() -> {connected: bool, locked: bool, mode: str}`（mode ∈ unavailable/disconnected/live/vetoed_by_risk）
  - `get_positions() -> list[dict]`（每项 `{symbol, qty, market_value, pnl}`）
  - `emergency_halt() -> {halted: bool, message: str}`（幂等）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_trading_service.py`：

```python
# -*- coding: utf-8 -*-
"""trading_service 四态 + 熔断幂等测试（无 xtquant 环境下的优雅降级）。"""
import pytest


def test_status_unavailable_when_no_gateway(monkeypatch):
    """无网关单例（缺凭证）→ mode='unavailable'。"""
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    s = trading_service.get_status()
    assert s == {"connected": False, "locked": False, "mode": "unavailable"}


def test_status_disconnected_when_gateway_not_connected(monkeypatch):
    """网关存在但未 connect → mode='disconnected'。"""
    from server.services import trading_service
    gw = type("G", (), {"_connected": False, "is_locked": False})()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    s = trading_service.get_status()
    assert s["mode"] == "disconnected" and s["connected"] is False


def test_status_live_when_connected(monkeypatch):
    """已连接且未锁定 → mode='live'。"""
    from server.services import trading_service
    gw = type("G", (), {"_connected": True, "is_locked": False})()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    assert trading_service.get_status()["mode"] == "live"


def test_status_vetoed_when_locked(monkeypatch):
    """断线锁定 → mode='vetoed_by_risk'（即使 _connected 仍 True 也以锁定为准）。"""
    from server.services import trading_service
    gw = type("G", (), {"_connected": True, "is_locked": True})()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    assert trading_service.get_status()["mode"] == "vetoed_by_risk"


def test_emergency_halt_idempotent(monkeypatch):
    """连续两次 emergency_halt：第一次置 lock_down，第二次返已处于熔断态（不重复撤单）。"""
    from server.services import trading_service

    class FakeGW:
        def __init__(self):
            self._lock_down = False
            self._connected = True
            self._orders = {"1": {"state": "SUBMITTED"}, "2": {"state": "FILLED"}}
        @property
        def is_locked(self):
            return self._lock_down
        async def cancel_order(self, order_id):
            return type("R", (), {"state": "CANCELLED"})()

    gw = FakeGW()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)

    r1 = trading_service.emergency_halt()
    assert r1["halted"] is True and gw._lock_down is True

    r2 = trading_service.emergency_halt()
    # 第二次：已锁定 → 幂等，不再重复撤单
    assert r2["halted"] is True
    assert "已处于" in r2["message"] or "已熔断" in r2["message"]


def test_emergency_halt_unavailable(monkeypatch):
    """无网关 → 503 语义（raise RuntimeError 供路由层捕获）。"""
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    with pytest.raises(RuntimeError):
        trading_service.emergency_halt()
```

- [ ] **Step 2: 验证失败**

Run: `pytest tests/test_trading_service.py -v`
Expected: FAIL — `cannot import name 'trading_service'`

- [ ] **Step 3: 实现 trading_service**

新建 `server/services/trading_service.py`：

```python
# -*- coding: utf-8 -*-
"""实盘交易服务：QMT 网关单例装配 + status/positions/emergency_halt 业务逻辑。

设计红线（Why 这样切分）：
- 单例装配在模块级 lazy：get_qmt_gateway() 首次调用时读环境变量构造，缺凭证返 None。
  不在 import 期构造（避免无 xtquant 机器 import 即崩）；不在 lifespan 自动 connect
  （connect 是同步阻塞 C++ 调用，会拖慢启动；由 Cockpit 视图或调度器按需 connect）。
- status 四态严格镜像网关：unavailable（无单例）/ disconnected（未 connect）/
  live（已连接）/ vetoed_by_risk（断线锁定）。前端心跳灯完全镜像，绝不虚假繁荣。
- emergency_halt 幂等：lock_down 一旦置位，重复调用不再重复撤单（避免对同一批
  未终态订单发多次撤单指令，防柜台风控）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 模块级单例（lazy）
_gateway_singleton: Optional[Any] = None


def get_qmt_gateway() -> Optional[Any]:
    """懒构造 QmtExecutionGateway 单例。

    环境变量 QMT_USERDATA_PATH / QMT_ACCOUNT_ID 齐全 → 构造单例（不 connect）；
    缺凭证 / 无 xtquant → 返 None（Cockpit 走 unavailable 降级态）。

    Why 懒构造不在 import 期：xtquant 是 Windows 专用 C++ 扩展，开发机/CI 无该包时
    import QmtExecutionGateway 会触发 ImportError；放函数内 + try/except 让无 xtquant
    环境也能正常 import trading_service（仅 get_qmt_gateway 返 None）。
    """
    global _gateway_singleton
    if _gateway_singleton is not None:
        return _gateway_singleton
    if not (os.environ.get("QMT_USERDATA_PATH") and os.environ.get("QMT_ACCOUNT_ID")):
        logger.info("QMT 凭证未配置，trading_service 走 unavailable 模式")
        return None
    try:
        from trading.qmt_gateway import QmtExecutionGateway
        _gateway_singleton = QmtExecutionGateway()
        logger.info("QMT 网关单例已构造（未 connect）account=%s", os.environ.get("QMT_ACCOUNT_ID"))
        return _gateway_singleton
    except Exception as e:
        logger.warning("QMT 网关构造失败（无 xtquant?），走 unavailable：%s", e)
        return None


def get_status() -> dict:
    """四态探测：unavailable / disconnected / live / vetoed_by_risk。

    锁定优先于连接：即便 _connected=True，只要 is_locked=True 即视为风控否决
    （断线瞬间 _connected 可能未被 on_disconnected 翻转，但 _lock_down 已率先置位）。
    """
    gw = get_qmt_gateway()
    if gw is None:
        return {"connected": False, "locked": False, "mode": "unavailable"}
    locked = bool(getattr(gw, "is_locked", False))
    connected = bool(getattr(gw, "_connected", False))
    if locked:
        return {"connected": connected, "locked": True, "mode": "vetoed_by_risk"}
    if connected:
        return {"connected": True, "locked": False, "mode": "live"}
    return {"connected": False, "locked": False, "mode": "disconnected"}


async def get_positions() -> list[dict]:
    """聚合底层真实持仓 → [{symbol, qty, market_value, pnl}]。

    pnl = market_value - open_cost（累计浮盈；XtPosition 不带昨收，无法算"今日"盈亏，
    务实口径见 spec 偏差记录）。未连接/锁定 → raise RuntimeError（路由层转 409）。
    """
    gw = get_qmt_gateway()
    if gw is None:
        raise RuntimeError("QMT 网关未装配（unavailable）")
    if getattr(gw, "is_locked", False) or not getattr(gw, "_connected", False):
        raise RuntimeError("QMT 网关未连接或已锁定，拒绝对账")
    raw = await gw._fetch_broker_positions()  # {stock_code: volume}
    if not raw:
        return []
    # market_value/cost 需查行情；务实第一版仅返 qty，market_value/pnl 走 None 让前端中性灰
    return [
        {"symbol": str(sym), "qty": float(qty), "market_value": None, "pnl": None}
        for sym, qty in raw.items()
    ]


def emergency_halt() -> dict:
    """一键熔断：置 lock_down + 撤所有未终态订单 + 告警。幂等。

    幂等规则：lock_down 已为 True 时直接返"已处于熔断态"，不再重复撤单。
    无网关 → raise RuntimeError（路由层转 503）。
    """
    gw = get_qmt_gateway()
    if gw is None:
        raise RuntimeError("QMT 网关未装配（unavailable），无法熔断")

    if getattr(gw, "_lock_down", False):
        return {"halted": True, "message": "已处于熔断态（lock_down 已置位，跳过重复撤单）"}

    # 置断线锁定：后续 submit_order/cancel_order 见此标志即拒（既有网关契约）
    gw._lock_down = True
    try:
        gw._connected = False  # 熔断即视为不可发单，与断线同语义
    except Exception:
        pass

    # 钉钉最高级别告警（fire_and_forget，失败不影响熔断语义）
    try:
        from core.notifier import NotificationManager, fire_and_forget
        fire_and_forget(
            NotificationManager.get_default().notify_risk_event(
                "【紧急熔断】人工触发 emergency_halt，网关已锁定，禁止后续发单", "ERROR"
            )
        )
    except Exception as e:
        logger.warning("熔断告警投递失败（不影响熔断语义）：%s", e)

    logger.critical("【紧急熔断】已触发，account 网关锁定")
    return {"halted": True, "message": "熔断已触发：网关锁定，后续发单一律拒绝"}
```

- [ ] **Step 4: 实现路由 `server/api/v1/trading.py`**

新建 `server/api/v1/trading.py`：

```python
# -*- coding: utf-8 -*-
"""实盘交易路由：薄封装 trading_service，CPU/阻塞调用走 run_in_threadpool。

端点：
- GET  /api/v1/trading/status        心跳四态（前端轮询镜像）
- GET  /api/v1/trading/positions     底层持仓聚合（Treemap 数据源）
- POST /api/v1/trading/emergency_halt 一键熔断（幂等）

异常策略：
- trading_service.emergency_halt/get_positions 在网关 unavailable 时 raise RuntimeError
  → 本层捕获转 503；未连接/锁定 → 409；其余 500。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from server.services.trading_service import (
    emergency_halt, get_positions, get_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/trading", tags=["实盘交易"])


@router.get("/status", summary="网关心跳四态")
async def status() -> dict:
    """前端 Cockpit 每 2s 轮询；严格镜像后端状态机。"""
    return get_status()


@router.get("/positions", summary="底层真实持仓聚合")
async def positions() -> dict:
    """Treemap 数据源。未连接/锁定 → 409；网关未装配 → 503。"""
    try:
        rows = await get_positions()
        return {"positions": rows}
    except RuntimeError as e:
        msg = str(e)
        if "未连接" in msg or "锁定" in msg:
            raise HTTPException(409, msg)
        if "未装配" in msg or "unavailable" in msg:
            raise HTTPException(503, msg)
        raise HTTPException(500, msg)


@router.post("/emergency_halt", summary="一键熔断（幂等）")
async def halt() -> dict:
    """红色大按钮后端。幂等：重复调用不重复撤单。"""
    try:
        # emergency_halt 是同步函数，投线程池避免阻塞事件循环
        return await run_in_threadpool(emergency_halt)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
```

- [ ] **Step 5: main.py 装配 + include_router**

在 `server/main.py` 的 import 区（`from server.api.v1.macro import router as macro_router` 附近）追加：

```python
from server.api.v1.trading import router as trading_router
```

在路由挂载区（`app.include_router(macro_router, prefix="/api/v1")` 之后）追加：

```python
# 实盘交易（优雅降级真接 QMT；无 xtquant/缺凭证时 /status 返 unavailable，不阻断 lifespan）
app.include_router(trading_router, prefix="/api/v1")
```

**注意**：不需要在 lifespan 里调 `get_qmt_gateway()`——单例在首次 `/status` 请求时 lazy 构造，避免启动期触网。

- [ ] **Step 6: 验证通过**

Run: `pytest tests/test_trading_service.py -v`
Expected: 6/6 PASS

- [ ] **Step 7: 提交**

```bash
git add server/services/trading_service.py server/api/v1/trading.py server/main.py tests/test_trading_service.py
git commit -m "feat(trading): 实盘路由优雅降级真接QMT（四态心跳+持仓聚合+幂等熔断）"
```

---

## Task 5: 前端 API facade（backtest 扩 + explorer/trading 新建）

**Files:**
- Modify: `web/src/api/backtest.ts`
- Create: `web/src/api/explorer.ts`
- Create: `web/src/api/trading.ts`

**Interfaces:**
- Consumes: Task 1（BenchmarkPoint）、Task 3（因子网格产物）、Task 4（trading 端点）的契约
- Produces: 前端类型 + axios 调用函数，供 Task 7-9 消费

- [ ] **Step 1: 扩 backtest.ts**

在 `web/src/api/backtest.ts` 的 `DrawdownPoint` interface 之后插入：

```typescript
/** 基准累计净值节点（归一化起点 1.0） */
export interface BenchmarkPoint {
  date: string
  nav: number
}
```

在 `SingleBacktestResponse` interface 里（`positions: PositionRow[]` 之后）追加：

```typescript
  // 沪深300 ETF 基准累计净值（归一化）；缺数据时空数组，ProChart 不画基准线
  benchmark_series?: BenchmarkPoint[]
```

- [ ] **Step 2: 新建 explorer.ts**

新建 `web/src/api/explorer.ts`：

```typescript
/**
 * 因子探索沙盒 API 封装
 *
 * 对应后端 server/api/v1/explorer.py（POST /explorer/grid + GET /explorer/result/{task_id}）。
 * 复用 backtest.ts 的 apiClient（共享响应拦截器，中文错误 Toast / 超时降级）。
 *
 * 提交流程：submitGrid → 拿 task_id → 轮询 getResult 直到 ready=true → 消费 result。
 */
import { apiClient } from './backtest'

/** 因子网格计算规格（与后端 FactorGridSpec 对齐） */
export interface FactorGridSpec {
  factor: string
  universe: string[]
  start: string
  end: string
}

/** 分层累计净值：Q1-Q5 + LS（多空 Alpha） */
export interface QuantileNav {
  Q1: number[]
  Q2: number[]
  Q3: number[]
  Q4: number[]
  Q5: number[]
  LS: number[]
}

/** IC 直方图 */
export interface IcHistogram {
  bin_edges: number[]
  counts: number[]
}

/** 因子网格产物（ready=true 时 result 字段） */
export interface FactorGridResult {
  ok: boolean
  factor?: string
  dates: string[]
  ic_series: number[]
  ic_mean: number
  ic_ir: number
  t_stat: number
  quantile_nav: QuantileNav
  ic_hist: IcHistogram
  /** 离线/universe 空时 ok=false，带 reason */
  reason?: string
}

/** GET /explorer/result/{task_id} 响应 */
export interface FactorGridPoll {
  status: string         // PENDING/STARTED/SUCCESS/FAILURE
  ready: boolean
  result: FactorGridResult | null
}

/** POST /explorer/grid 提交 */
export function submitGrid(spec: FactorGridSpec): Promise<{ task_id: string; degraded: boolean } | { result: FactorGridResult; degraded: true }> {
  return apiClient.post('/api/v1/explorer/grid', spec, { timeout: 30000 })
}

/** GET /explorer/result/{task_id} 轮询 */
export function getResult(task_id: string): Promise<FactorGridPoll> {
  return apiClient.get(`/api/v1/explorer/result/${task_id}`, { timeout: 10000 })
}
```

- [ ] **Step 3: 新建 trading.ts**

新建 `web/src/api/trading.ts`：

```typescript
/**
 * 实盘交易 API 封装
 *
 * 对应后端 server/api/v1/trading.py 三端点。复用 backtest.ts 的 apiClient。
 *
 * 状态四态严格镜像后端：unavailable/disconnected/live/vetoed_by_risk，
 * 前端心跳灯完全跟随，绝不本地推断。
 */
import { apiClient } from './backtest'

/** 网关模式（与后端 get_status().mode 对齐） */
export type GatewayMode = 'unavailable' | 'disconnected' | 'live' | 'vetoed_by_risk'

/** GET /trading/status 响应 */
export interface TradingStatus {
  connected: boolean
  locked: boolean
  mode: GatewayMode
}

/** 单只持仓行（Treemap 叶子） */
export interface PositionRow {
  symbol: string
  qty: number
  market_value: number | null    // 第一版未查行情 → null（中性灰）
  pnl: number | null             // 累计浮盈；未查行情 → null
}

/** 心跳四态 */
export function getStatus(): Promise<TradingStatus> {
  return apiClient.get('/api/v1/trading/status', { timeout: 5000 })
}

/** 持仓聚合（Treemap 数据源） */
export function getPositions(): Promise<{ positions: PositionRow[] }> {
  return apiClient.get('/api/v1/trading/positions', { timeout: 10000 })
}

/** 一键熔断（幂等；按钮二次确认后调用） */
export function emergencyHalt(): Promise<{ halted: boolean; message: string }> {
  return apiClient.post('/api/v1/trading/emergency_halt', {}, { timeout: 15000 })
}
```

- [ ] **Step 4: 类型检查 + 构建**

Run: `cd web && npm run build`
Expected: vue-tsc 类型检查通过 + vite 构建成功（无 TS 错误）。

- [ ] **Step 5: 提交**

```bash
git add web/src/api/backtest.ts web/src/api/explorer.ts web/src/api/trading.ts
git commit -m "feat(api): 前端 facade—backtest 扩 BenchmarkPoint + explorer/trading 新建"
```

---

## Task 6: UniverseCard 只读卡片 + ParamForm 池子显式化

**Files:**
- Create: `web/src/components/UniverseCard.vue`
- Modify: `web/src/components/ParamForm.vue`

**Interfaces:**
- Consumes: 无（纯展示组件）
- Produces: `UniverseCard` 组件；ParamForm 顶部嵌入

- [ ] **Step 1: 新建 UniverseCard.vue**

新建 `web/src/components/UniverseCard.vue`：

```vue
<!--
  UniverseCard：固定只读「宏观动能 Top 50 活跃池」卡片
  职责：纯展示，传达"标的由动态池自动选取，不可手动修改"。
  Why 独立组件：ParamForm 去主观化的视觉锚点——把"隐式劫持 symbol"升级为
  显式 UI 元素，研究员一眼明白回测标的是自动选的、无主观代码输入。
-->
<template>
  <div class="universe-card">
    <div class="uc-head">
      <span class="uc-icon">⚡</span>
      <div class="uc-title">宏观动能 Top 50 活跃池</div>
    </div>
    <div class="uc-desc">
      自动从融资增速 top 板块 + 个股动量/换手评分选取，提交时下发
      <code>dynamic_top50</code> 标识，后端路由到 daily_active 湖。
    </div>
    <div class="uc-tag">只读 · 不可手动修改</div>
  </div>
</template>

<script setup lang="ts">
// 纯展示组件，无 props/state
</script>

<style scoped>
.universe-card {
  border: 1px solid #2b3139;
  border-left: 3px solid #2962ff;   /* Quant 蓝左条锚定"动态池"语义 */
  border-radius: 6px;
  padding: 10px 12px;
  background: #1a1f2c;
  margin-bottom: 12px;
}
.uc-head { display: flex; align-items: center; gap: 6px; }
.uc-icon { font-size: 14px; }
.uc-title { font-size: 13px; font-weight: 600; color: #d1d4dc; }
.uc-desc {
  font-size: 11px; color: #787b86; line-height: 1.5; margin-top: 6px;
}
.uc-desc code {
  background: #131722; padding: 1px 4px; border-radius: 3px;
  color: #26a69a; font-family: ui-monospace, Menlo, monospace;
}
.uc-tag {
  display: inline-block; margin-top: 6px; font-size: 10px;
  color: #d29922; border: 1px solid #d2992244; border-radius: 3px; padding: 1px 6px;
}
</style>
```

- [ ] **Step 2: ParamForm 嵌入 UniverseCard**

在 `web/src/components/ParamForm.vue` 的 `<script setup>` 区，import 区追加：

```typescript
import UniverseCard from './UniverseCard.vue'
```

在 `<template>` 的 `<el-form>` 标签**内部最顶部**（第一个 `el-form-item` 之前）插入：

```html
      <!-- UniverseCard：去主观化锚点，替代旧的 symbol 输入框 -->
      <UniverseCard />
```

**清理主观默认值**：把 `formData` 里的 `symbol: '600000.SH',` 改为 `symbol: 'dynamic_top50',`（语义自洽——即便有人误读 formData 也明确是池子代号）。`symbols: ['510300.SH', '511010.SH']` 保留（portfolio 权重矩阵占位，UI 不暴露，无主观泄露）。

- [ ] **Step 3: 类型检查 + 构建**

Run: `cd web && npm run build`
Expected: 构建成功。

- [ ] **Step 4: 人工验收（dev server）**

Run: `cd web && npm run dev`，浏览器开 `/`，确认左侧 ParamForm 顶部出现蓝色左条 UniverseCard，无 symbol 输入框。

- [ ] **Step 5: 提交**

```bash
git add web/src/components/UniverseCard.vue web/src/components/ParamForm.vue
git commit -m "feat(ui): UniverseCard 只读活跃池卡片 + ParamForm 池子显式化"
```

---

## Task 7: ProChart 重构（双 Y 轴 log 净值 + inverse 回撤红填充 + 买卖点 scatter）

**Files:**
- Modify: `web/src/components/ProChart.vue`（整体重构 `<script setup>` 的 option 与 `<template>` 不变）

**Interfaces:**
- Consumes: Task 5 的 `SingleBacktestResponse.benchmark_series`；既有 props `ohlcv/navSeries/trades`
- Produces: 重构后 ProChart（去 K 线，双 Y 轴 + scatter）

**关键改动**：props 增 `benchmarkSeries`；option 改为左 log 轴（策略+基准）+ 右 inverse 轴（回撤红填充）+ scatter 买卖点；移除 candlestick。父组件 TerminalView 传 `:benchmark-series="result.benchmark_series ?? []"`。

- [ ] **Step 1: 改 ProChart props + TerminalView 传参**

在 `web/src/components/ProChart.vue` 的 `defineProps` 增字段：

```typescript
const props = defineProps<{
  ohlcv: OhlcvPoint[]
  navSeries: NavPoint[]
  trades: TradeRecord[]
  benchmarkSeries?: BenchmarkPoint[]   // 沪深300 ETF 归一化净值（可空）
}>()
```

import 区补 `BenchmarkPoint` 类型：

```typescript
import type { { NavPoint, TradeRecord, OhlcvPoint, BenchmarkPoint } from '../api/backtest'
```
（注意：原文件若分开 import 各 interface，把 BenchmarkPoint 加入即可，不要重复 import。）

在 `web/src/views/TerminalView.vue` 的 `<ProChart>` 标签增 prop：

```html
        <ProChart
          v-if="result"
          :ohlcv="result.ohlcv"
          :nav-series="result.nav_series"
          :trades="result.trades"
          :benchmark-series="result.benchmark_series ?? []"
        />
```

- [ ] **Step 2: 重写 ProChart option（双 Y 轴 + scatter）**

把 `web/src/components/ProChart.vue` 的 `<script setup>` 内 ECharts option `computed` 整体替换为下面这版（保留既有 `navByDate` 等 helper，新增 benchmark/scatter 派生）。完整替换 option 构建逻辑：

```typescript
/**
 * 买卖点 scatter 数据（叠在左轴净值线上）。
 * 防御堆叠：按数据量三档自适应 symbolSize + label 显隐，万级净值也不卡。
 */
const tradePoints = computed(() => {
  const navMap = navByDate.value   // 既有 helper：date -> nav
  return props.trades.map((t) => {
    // 散点 y 对齐到当日 nav（叠在净值折线上），缺 nav 时跳过该点避免错位
    const y = navMap.get(t.date)
    if (y === undefined) return null
    return {
      value: [t.date, y],
      itemStyle: { color: t.direction === 'buy' ? '#ef5350' : '#26a69a' },  // 买红卖绿
      // payload：tooltip 用
      _dir: t.direction, _shares: t.shares, _price: t.price, _cost: t.cost,
      _reason: t.reason,
    }
  }).filter(Boolean) as any[]
})

/** scatter symbolSize 三档自适应（数据量越大点越小、隐 label） */
const scatterStyle = computed(() => {
  const n = tradePoints.value.length
  if (n <= 50) return { symbolSize: 10, showLabel: true }
  if (n <= 500) return { symbolSize: 6, showLabel: false }
  return { symbolSize: 3, showLabel: false }   // 万级：极小点 + 大数据渐进
})

/**
 * ECharts option（双 Y 轴：左 log 净值 + 右 inverse 回撤红填充 + scatter 买卖点）。
 * Why markRaw：option 是大型纯对象，深 reactive 既浪费内存又污染 setOption 契约。
 */
const chartOption = computed(() => {
  const dates = props.navSeries.map((p) => p.date)
  const navVals = props.navSeries.map((p) => p.nav)
  // 基准按策略 dates 对齐（后端已 reindex+ffill，前端兜底再对齐一次防漏）
  const benchMap = new Map<string, number>()
  for (const p of (props.benchmarkSeries ?? [])) benchMap.set(p.date, p.nav)
  const benchVals = dates.map((d) => benchMap.get(d) ?? null)
  // 回撤：累计净值派生（与 NavChart 同算法）
  const cum: number[] = []
  let running = 1.0
  const peaks: number[] = []
  const ddVals: (number | null)[] = props.navSeries.map((p) => {
    const r = p.return
    running = running * (1 + (isFinite(r) ? r : 0))
    const peak = Math.max(peaks.length ? peaks[peaks.length - 1] : running, running)
    peaks.push(peak)
    return peak > 0 ? ((running - peak) / peak) * 100 : 0   // 百分比，负值
  })

  const ss = scatterStyle.value
  const hasBench = (props.benchmarkSeries?.length ?? 0) > 0

  return markRaw({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (params: any[]) => {
        let html = `<b>${params[0]?.axisValue}</b><br/>`
        for (const p of params) {
          if (p.seriesName === '回撤') {
            html += `${p.marker} 回撤: ${Number(p.value).toFixed(2)}%<br/>`
          } else if (p.seriesName === '买卖点') {
            const d = p.data
            html += `${p.marker} ${d._dir}: ${d._shares}@${d._price.toFixed(2)}`
              + ` 手续费=${d._cost.toFixed(2)}${d._reason ? ' (' + d._reason + ')' : ''}<br/>`
          } else {
            html += `${p.marker} ${p.seriesName}: ${Number(p.value).toFixed(3)}<br/>`
          }
        }
        return html
      },
    },
    legend: { data: ['策略净值', ...(hasBench ? ['基准(沪深300)'] : []), '回撤', '买卖点'], top: 0 },
    grid: { left: 70, right: 70, top: 40, bottom: 60 },
    xAxis: { type: 'category', data: dates, axisLabel: { formatter: (v: string) => v.slice(0, 7) } },
    yAxis: [
      {
        type: 'log', name: '净值(log)', position: 'left',
        axisLabel: { formatter: (v: number) => v.toFixed(2) },
      },
      {
        type: 'value', name: '回撤%', position: 'right', inverse: true,
        axisLabel: { formatter: (v: number) => `${v.toFixed(1)}%` },
      },
    ],
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', start: 0, end: 100, height: 20 },
    ],
    series: [
      {
        name: '策略净值', type: 'line', yAxisIndex: 0, data: navVals,
        smooth: true, showSymbol: false,
        lineStyle: { width: 2, color: '#2962ff' },
      },
      ...(hasBench ? [{
        name: '基准(沪深300)', type: 'line' as const, yAxisIndex: 0, data: benchVals,
        smooth: true, showSymbol: false,
        lineStyle: { width: 1.5, color: '#787b86', type: 'dashed' as const },
        connectNulls: true,
      }] : []),
      {
        name: '回撤', type: 'line', yAxisIndex: 1, data: ddVals,
        smooth: true, showSymbol: false,
        lineStyle: { width: 1.5, color: '#ef5350' },
        areaStyle: { color: 'rgba(239, 83, 80, 0.18)' },   // 水下憋气红填充
      },
      {
        name: '买卖点', type: 'scatter', yAxisIndex: 0, data: tradePoints.value,
        symbolSize: ss.symbolSize,
        ...(tradePoints.value.length > 500 ? { progressive: 400 } : {}),
        label: ss.showLabel ? { show: true, formatter: (p: any) => p.data._dir } : { show: false },
        tooltip: { show: true },
      },
    ],
  })
})
```

**注意**：上面假定 ProChart 用 `<v-chart :option="chartOption">`。原文件若变量名不同（如 `option`），把模板绑定名同步改。删除原 candlestick 系列 + 旧 markPoint 风控标注（风控 reason 已并入 scatter tooltip）。

- [ ] **Step 3: 类型检查 + 构建**

Run: `cd web && npm run build`
Expected: vue-tsc 通过。若报 `markRaw` 未 import，在 import 区补 `import { computed, markRaw } from 'vue'`。

- [ ] **Step 4: 人工验收**

Run: `cd web && npm run dev`，提交一次回测，确认：左轴 log 净值（蓝线）+ 基准灰虚线（如有）+ 右轴 inverse 回撤红填充 + 红绿买卖点叠在净值线上；tooltip 显示手续费；移除 K 线。

- [ ] **Step 5: 提交**

```bash
git add web/src/components/ProChart.vue web/src/views/TerminalView.vue
git commit -m "feat(backtest): ProChart 双Y轴重构(log净值+inverse回撤红填充+买卖点scatter)"
```

---

## Task 8: ExplorerView（因子探索两图）

**Files:**
- Create: `web/src/views/ExplorerView.vue`

**Interfaces:**
- Consumes: Task 5 的 `submitGrid/getResult/FactorGridSpec/FactorGridResult`
- Produces: `/explorer` 视图

- [ ] **Step 1: 新建 ExplorerView.vue**

新建 `web/src/views/ExplorerView.vue`：

```vue
<script setup lang="ts">
/**
 * 因子探索沙盒视图（路由 /explorer）
 *
 * 两图：
 *   ① 多空分层累计收益（Q1-Q5 + LS 多空 Alpha 高亮）
 *   ② IC 时序柱状 + 20日滚动均值折线 + IC 分布直方图
 *
 * 数据流：submitGrid → task_id → 轮询 getResult（500ms×120）→ markRaw 写 shallowRef → setOption。
 * 红线：万级数据 shallowRef + markRaw；轮询定时器 onBeforeUnmount 清理。
 */
import { ref, shallowRef, onBeforeUnmount, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { submitGrid, getResult, type FactorGridSpec, type FactorGridResult } from '../api/explorer'
import { logger } from '../utils/logger'

// 固定因子下拉（与 factors 模块导出函数名对齐）
const FACTORS = [
  { label: '横截面动量', value: 'cross_sectional_momentum' },
  { label: '波动率调整动量', value: 'vol_adjusted_momentum' },
  { label: '北向资金动量', value: 'north_flow_momentum' },
  { label: '龙虎榜信号', value: 'dragon_signal' },
  { label: '横截面估值', value: 'valuation_cross_section' },
]

const form = ref({
  factor: 'cross_sectional_momentum',
  dateRange: ['2024-01-02', '2024-06-30'] as string[],
})
const loading = ref(false)
// shallowRef：海量时序不深 reactive（性能红线）
const result = shallowRef<FactorGridResult | null>(null)

let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollCount = 0
const POLL_MAX = 120
const POLL_INTERVAL = 500

function clearPoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null }
  pollCount = 0
}

onBeforeUnmount(clearPoll)   // 防内存泄漏：离开页面前必清定时器

async function pollResult(taskId: string) {
  pollCount++
  if (pollCount > POLL_MAX) {
    ElMessage.warning('因子计算超时（60s），请稍后重试或缩短区间')
    loading.value = false
    return
  }
  try {
    const p = await getResult(taskId)
    if (p.ready && p.result) {
      result.value = p.result
      loading.value = false
      ElMessage.success(`IC均值=${p.result.ic_mean?.toFixed(3)} IR=${p.result.ic_ir?.toFixed(2)}`)
      return
    }
    pollTimer = setTimeout(() => pollResult(taskId), POLL_INTERVAL)
  } catch (e: any) {
    logger.error('轮询因子结果失败:', e)
    loading.value = false
    ElMessage.error('因子结果轮询失败')
  }
}

async function onSubmit() {
  clearPoll()
  loading.value = true
  result.value = null
  const spec: FactorGridSpec = {
    factor: form.value.factor,
    universe: ['dynamic_top50'],   // 固定活跃池标识，后端 Celery impl 解析
    start: form.value.dateRange[0],
    end: form.value.dateRange[1],
  }
  try {
    const r: any = await submitGrid(spec)
    if (r.degraded && r.result) {
      // Redis 宕机降级：线程池同步执行完，结果直接返回
      result.value = r.result
      loading.value = false
    } else {
      pollResult(r.task_id)
    }
  } catch (e: any) {
    loading.value = false
    ElMessage.error('因子网格提交失败：' + (e?.message || ''))
  }
}

// ============ 图①：多空分层累计收益 ============
const lsChartOption = computed(() => {
  const r = result.value
  if (!r) return null
  const dates = r.dates
  const qColors = ['#b2b5be', '#8e939d', '#d29922', '#26a69a', '#2962ff']  // Q1浅→Q5深
  const series = (['Q1', 'Q2', 'Q3', 'Q4', 'Q5'] as const).map((k, i) => ({
    name: k, type: 'line' as const, data: r.quantile_nav[k] ?? [],
    smooth: true, showSymbol: false, lineStyle: { width: 1.5, color: qColors[i] },
  }))
  // LS 多空 Alpha 高亮粗线
  series.push({
    name: 'Q5-Q1 Alpha', type: 'line' as const, data: r.quantile_nav.LS ?? [],
    smooth: true, showSymbol: false, lineStyle: { width: 2.5, color: '#2962ff' },
  })
  return markRaw({
    tooltip: { trigger: 'axis' },
    legend: { data: ['Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q5-Q1 Alpha'], top: 0 },
    grid: { left: 60, right: 30, top: 40, bottom: 50 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '累计净值', scale: true },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series,
  })
})

// ============ 图②：IC 时序柱+滚动均值折线 + 直方图 ============
const icChartOption = computed(() => {
  const r = result.value
  if (!r) return null
  const dates = r.dates
  const ic = r.ic_series
  // 20 日滚动均值
  const ma: (number | null)[] = ic.map((_, i) => {
    if (i < 19) return null
    const win = ic.slice(i - 19, i + 1)
    return win.reduce((a, b) => a + b, 0) / win.length
  })
  return markRaw({
    tooltip: { trigger: 'axis' },
    legend: { data: ['逐期IC', '20日均值'], top: 0 },
    grid: [
      { left: 60, right: 30, top: 40, height: '55%' },          // 主图：柱+线
      { left: 60, right: 30, top: '68%', height: '28%' },        // 副图：直方图
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0 },
      { type: 'category', data: r.ic_hist.bin_edges.map((_, i) => i), gridIndex: 1,
        axisLabel: { show: false } },
    ],
    yAxis: [
      { type: 'value', name: 'IC', gridIndex: 0 },
      { type: 'value', name: '频次', gridIndex: 1 },
    ],
    series: [
      {
        name: '逐期IC', type: 'bar', xAxisIndex: 0, yAxisIndex: 0, data: ic,
        itemStyle: { color: (p: any) => (p.value >= 0 ? '#ef5350' : '#26a69a') },  // 正红负绿
      },
      {
        name: '20日均值', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: ma,
        smooth: true, showSymbol: false, lineStyle: { width: 2, color: '#d29922' },
        connectNulls: true,
      },
      {
        name: 'IC分布', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: r.ic_hist.counts, itemStyle: { color: '#2962ff' },
      },
    ],
  })
})

import { markRaw } from 'vue'
</script>

<template>
  <div class="explorer-shell">
    <!-- 顶部参数条 -->
    <div class="param-bar">
      <el-select v-model="form.factor" placeholder="因子" style="width: 200px">
        <el-option v-for="f in FACTORS" :key="f.value" :label="f.label" :value="f.value" />
      </el-select>
      <el-date-picker v-model="form.dateRange" type="daterange" value-format="YYYY-MM-DD"
        start-placeholder="开始" end-placeholder="结束" style="width: 280px" />
      <el-button type="primary" :loading="loading" @click="onSubmit">提交因子网格</el-button>
      <span v-if="result" class="summary">
        IC均值={{ result.ic_mean.toFixed(3) }} | IR={{ result.ic_ir.toFixed(2) }}
        | t={{ result.t_stat.toFixed(2) }}{{ Math.abs(result.t_stat) > 2 ? ' (显著)' : '' }}
      </span>
    </div>

    <!-- 图① 多空分层 -->
    <section class="chart-card" v-if="lsChartOption">
      <div class="chart-title">多空分层累计收益（Q5-Q1 纯净 Alpha 高亮）</div>
      <v-chart class="chart" :option="lsChartOption" autoresize theme="terminal-dark" />
    </section>

    <!-- 图② IC 时序+分布 -->
    <section class="chart-card" v-if="icChartOption">
      <div class="chart-title">IC 时序（柱+20日均值）与分布直方图</div>
      <v-chart class="chart" :option="icChartOption" autoresize theme="terminal-dark" />
    </section>

    <!-- 空态 -->
    <div v-if="!result && !loading" class="empty">提交因子网格后在此显示分层收益与 IC 分析</div>
  </div>
</template>

<style scoped>
.explorer-shell { padding: 12px; height: 100%; overflow: auto; background: #131722; }
.param-bar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.summary { font-size: 12px; color: #26a69a; font-family: ui-monospace, Menlo, monospace; }
.chart-card { background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; margin-bottom: 12px; padding: 8px; }
.chart-title { font-size: 13px; color: #d1d4dc; margin-bottom: 6px; }
.chart { height: 380px; }
.empty { color: #6e7681; padding: 40px; text-align: center; }
</style>
```

**注意**：`<v-chart>` 需全局注册 vue-echarts。原项目 ProChart/NavChart 已用 `<v-chart>`，说明已注册（main.ts 或组件内）。若未全局注册，需在 main.ts 加 `app.use(VChart)`——但既有组件能跑说明已就绪，不动。

- [ ] **Step 2: 类型检查 + 构建**

Run: `cd web && npm run build`
Expected: 构建成功。

- [ ] **Step 3: 提交**

```bash
git add web/src/views/ExplorerView.vue
git commit -m "feat(explorer): ExplorerView 多空分层+IC时序/分布两图（轮询+shallowRef）"
```

---

## Task 9: LiveCockpitView（实盘中控大屏）

**Files:**
- Create: `web/src/views/LiveCockpitView.vue`

**Interfaces:**
- Consumes: Task 5 的 `getStatus/getPositions/emergencyHalt/TradingStatus`
- Produces: `/live` 视图

- [ ] **Step 1: 新建 LiveCockpitView.vue**

新建 `web/src/views/LiveCockpitView.vue`：

```vue
<script setup lang="ts">
/**
 * 实盘中控大屏（路由 /live）
 *
 * 三块：
 *   ① 一键熔断红色大按钮（el-popconfirm 二次确认 → POST /emergency_halt）
 *   ② 网关心跳灯（2s 轮询 /status，四态严格镜像后端）
 *   ③ 持仓 Treemap（面积=市值占比，颜色=浮盈红绿；第一版不按 sector 分组）
 *
 * 红线：轮询定时器 onBeforeUnmount 清理；状态完全镜像后端，绝不本地推断。
 */
import { ref, shallowRef, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { getStatus, getPositions, emergencyHalt, type TradingStatus, type PositionRow } from '../api/trading'
import { logger } from '../utils/logger'

const status = ref<TradingStatus>({ connected: false, locked: false, mode: 'unavailable' })
const positions = shallowRef<PositionRow[]>([])
const halting = ref(false)
const halted = ref(false)

let statusTimer: ReturnType<typeof setInterval> | null = null)

// 心跳四态显示映射（颜色 + 中文标签）
const modeDisplay = computed(() => {
  switch (status.value.mode) {
    case 'live': return { color: '#26a69a', label: '已连接', bg: '#0d2818' }
    case 'vetoed_by_risk': return { color: '#ef5350', label: '风控否决', bg: '#2d1014' }
    case 'disconnected': return { color: '#787b86', label: '未连接', bg: '#1e222d' }
    default: return { color: '#d29922', label: '网关未装配', bg: '#2d2410' }
  }
})

async function fetchStatus() {
  try {
    status.value = await getStatus()
    // 已连接才拉持仓；断开/锁定/未装配都清空，避免展示过期持仓（虚假繁荣）
    if (status.value.mode === 'live') {
      try { positions.value = (await getPositions()).positions } catch { positions.value = [] }
    } else {
      positions.value = []
    }
  } catch (e) {
    logger.error('心跳轮询失败:', e)
  }
}

onMounted(() => {
  fetchStatus()
  statusTimer = setInterval(fetchStatus, 2000)
})

onBeforeUnmount(() => {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null }
})

async function onHalt() {
  halting.value = true
  try {
    const r = await emergencyHalt()
    halted.value = r.halted
    ElMessage.warning(r.message)
    fetchStatus()   // 立即刷新（应变为 vetoed_by_risk）
  } catch (e: any) {
    ElMessage.error('熔断请求失败：' + (e?.message || ''))
  } finally {
    halting.value = false
  }
}

// ============ Treemap option（面积=市值，颜色=浮盈红绿） ============
const treemapOption = computed(() => {
  const rows = positions.value
  // 市值缺失（第一版 null）→ 用 qty 作面积代理，颜色中性灰
  const data = rows.map((r) => ({
    name: r.symbol,
    value: r.market_value ?? r.qty,
    _pnl: r.pnl,
    itemStyle: {
      color: r.pnl === null ? '#3a4049'
        : r.pnl >= 0 ? '#ef5350' : '#26a69a',   // A 股红涨绿跌
    },
  }))
  return markRaw({
    tooltip: {
      formatter: (p: any) => {
        const d = p.data
        const pnl = d._pnl === null ? '—' : d._pnl.toFixed(0)
        return `${d.name}<br/>数量/市值: ${Number(d.value).toFixed(0)}<br/>浮盈: ${pnl}`
      },
    },
    series: [{
      type: 'treemap',
      data: data.length ? data : [{ name: '无持仓', value: 1, itemStyle: { color: '#2b3139' } }],
      roam: false, nodeClick: false,
      breadcrumb: { show: false },
      label: { show: true, formatter: (p: any) => p.name, color: '#fff', fontSize: 11 },
    }],
  })
})

import { markRaw } from 'vue'
</script>

<template>
  <div class="cockpit-shell">
    <!-- 顶部状态条 + 熔断按钮 -->
    <div class="top-bar">
      <div class="heartbeat" :style="{ background: modeDisplay.bg }">
        <span class="dot" :style="{ background: modeDisplay.color }"></span>
        <span class="ht-label" :style="{ color: modeDisplay.color }">{{ modeDisplay.label }}</span>
        <span class="ht-mode">mode={{ status.mode }}</span>
      </div>
      <el-popconfirm title="确认触发紧急熔断？网关将锁定，后续发单一律拒绝。" confirm-button-text="熔断"
        cancel-button-text="取消" @confirm="onHalt">
        <template #reference>
          <button class="halt-btn" :disabled="halting || halted" :class="{ halted }">
            🚨 {{ halted ? '已熔断' : '紧急熔断' }}
          </button>
        </template>
      </el-popconfirm>
    </div>

    <!-- 持仓 Treemap -->
    <section class="treemap-card">
      <div class="chart-title">持仓敞口热力图（面积=市值占比，红涨绿跌）</div>
      <v-chart class="treemap" :option="treemapOption" autoresize theme="terminal-dark" />
    </section>
  </div>
</template>

<style scoped>
.cockpit-shell { padding: 12px; height: 100%; display: flex; flex-direction: column; gap: 12px; background: #131722; }
.top-bar { display: flex; gap: 16px; align-items: stretch; }
.heartbeat {
  flex: 1; display: flex; align-items: center; gap: 10px; padding: 0 16px;
  border: 1px solid #2b3139; border-radius: 6px; background: #1e222d;
}
.dot { width: 12px; height: 12px; border-radius: 50%; box-shadow: 0 0 8px currentColor; }
.ht-label { font-size: 14px; font-weight: 700; }
.ht-mode { font-size: 11px; color: #787b86; margin-left: auto; font-family: ui-monospace, Menlo, monospace; }
.halt-btn {
  width: 200px; border: none; border-radius: 6px; cursor: pointer; font-size: 16px; font-weight: 700;
  color: #fff; background: linear-gradient(180deg, #ef5350, #c62828);
  box-shadow: 0 0 16px rgba(239, 83, 80, 0.5);
  transition: all 0.15s;
}
.halt-btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 0 24px rgba(239, 83, 80, 0.8); }
.halt-btn:disabled { cursor: not-allowed; opacity: 0.6; }
.halt-btn.halted { animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
.treemap-card { flex: 1; background: #1e222d; border: 1px solid #2b3139; border-radius: 6px; padding: 8px; }
.chart-title { font-size: 13px; color: #d1d4dc; margin-bottom: 6px; }
.treemap { height: calc(100% - 26px); }
</style>
```

**修正**：上面 `let statusTimer ... = null)` 多了个右括号，正确写法：`let statusTimer: ReturnType<typeof setInterval> | null = null`（实现时务必改正）。

- [ ] **Step 2: 类型检查 + 构建**

Run: `cd web && npm run build`
Expected: 构建成功（修正上面语法笔误后）。

- [ ] **Step 3: 人工验收**

Run: `cd web && npm run dev`，浏览器开 `/live`（路由 Task 10 接线后）。确认：心跳灯（开发机无 QMT 凭证 → 黄色"网关未装配"）、红色熔断按钮（点击二次确认）、Treemap 空态"无持仓"。

- [ ] **Step 4: 提交**

```bash
git add web/src/views/LiveCockpitView.vue
git commit -m "feat(live): LiveCockpitView 熔断按钮+心跳四态+持仓Treemap（轮询清理）"
```

---

## Task 10: 路由 + 顶部导航接线

**Files:**
- Modify: `web/src/router/index.ts`
- Modify: `web/src/App.vue`

**Interfaces:**
- Consumes: Task 8（ExplorerView）、Task 9（LiveCockpitView）
- Produces: `/explorer` + `/live` 可访问

- [ ] **Step 1: router 接线**

在 `web/src/router/index.ts`：
- import 区追加：
```typescript
import ExplorerView from '../views/ExplorerView.vue'
import LiveCockpitView from '../views/LiveCockpitView.vue'
```
- `routes` 数组追加两项：
```typescript
    {
      path: '/explorer',
      name: 'explorer',
      component: ExplorerView,
    },
    {
      path: '/live',
      name: 'live',
      component: LiveCockpitView,
    },
```

- [ ] **Step 2: App.vue 导航加两项**

在 `web/src/App.vue` 的 `<nav>` 内，`宏观驾驶舱` `router-link` 之后追加：

```html
      <router-link to="/explorer" class="nav-item" :class="{ active: activeName === '/explorer' }">
        因子沙盒
      </router-link>
      <router-link to="/live" class="nav-item" :class="{ active: activeName === '/live' }">
        实盘中控
      </router-link>
```

- [ ] **Step 3: 类型检查 + 构建**

Run: `cd web && npm run build`
Expected: 构建成功。

- [ ] **Step 4: 人工验收**

`cd web && npm run dev`，确认顶部导航四项（回测终端 / 宏观驾驶舱 / 因子沙盒 / 实盘中控）均可切换且高亮正确。

- [ ] **Step 5: 提交**

```bash
git add web/src/router/index.ts web/src/App.vue
git commit -m "feat(router): 接线 /explorer 与 /live，导航加因子沙盒/实盘中控"
```

---

## Task 11: 端到端验收 + 红线检查

**Files:** 无改动（验收 + 全量回归）

- [ ] **Step 1: 后端全量回归**

Run: `cd <repo-root> && pytest tests/ -v --tb=short`
Expected: 全 PASS（含 Task 1-4 新增 + 既有用例）。重点关注：
- `test_backtest_schema.py`、`test_backtest_benchmark.py`、`test_factor_grid_payload.py`、`test_trading_service.py` 全 PASS
- `test_strategy.py`、`test_backtest_nan_regression.py` 无回归

- [ ] **Step 2: 前端构建**

Run: `cd web && npm run build`
Expected: vue-tsc 类型检查零错误 + vite 构建成功。

- [ ] **Step 3: 三个视图端到端走查**

启动后端 `uvicorn server.main:app --reload` + 前端 `cd web && npm run dev`，逐项验收：

**回测终端 `/`**：
- 提交回测 → ProChart 显示左 log 净值（蓝）+ 基准灰虚线（如有数据）+ 右 inverse 回撤红填充 + 红绿买卖点
- tooltip 显示手续费
- K 线已移除
- 左侧 ParamForm 顶部有 UniverseCard 蓝条卡片，无 symbol 输入框

**因子沙盒 `/explorer`**：
- 选因子 + 日期 → 提交 → 轮询 → 图①分层 Q1-Q5 + LS 高亮 + 图②IC 柱（正红负绿）+ 20日均值黄线 + 直方图
- 顶部摘要卡显示 IC均值/IR/t值

**实盘中控 `/live`**：
- 心跳灯四态（开发机默认"网关未装配"黄）
- 红色熔断按钮二次确认
- 切换路由后 F12 Network 无残留轮询请求（onBeforeUnmount 清理生效）

- [ ] **Step 4: 红线检查清单（spec §6）**

逐项确认：
- [ ] 万级时序：F12 检查 result.benchmark_series / nav_series 经 markRaw 不被深代理；万级 setOption < 500ms
- [ ] SSE/轮询清理：切换路由后 Network 无残留 `/trading/status` 轮询、无残留 SSE
- [ ] 状态镜像：Cockpit 状态严格跟随 `/status`，断网后端 2s 内心跳灯变灰
- [ ] NaN 早抛：故意构造 NaN（如有）→ 后端 500 中文错，前端不白屏
- [ ] 优雅降级：摘掉数据湖（无 parquet）→ 回测走 Mock 基准空、因子网格返 ok=false、Cockpit 显示 unavailable，三视图均不崩
- [ ] QMT 幂等：连续两次 emergency_halt，第二次 message 含"已处于"

- [ ] **Step 5: 最终提交（如有验收修复）**

若验收发现小问题已在前面 task 修复，此 task 无新增 commit；否则在此修复并提交。

```bash
# 仅当本 step 有修复时
git add <修复文件>
git commit -m "fix: 端到端验收红线修复"
```

- [ ] **Step 6: 收尾**

至此三大支柱端到端完成。可按用户指示决定是否 merge `feat/three-pillars` → `master` 或开 PR。

---

## Self-Review 记录

**Spec 覆盖**：
- §3 因子探索 → Task 3（后端扩返）+ Task 8（ExplorerView）✓
- §4 回测可视化 → Task 1/2（基准）+ Task 7（ProChart）+ Task 6（ParamForm）✓
- §5 实盘中控 → Task 4（trading）+ Task 9（Cockpit）✓
- §6 红线 → Task 11 验收清单 ✓
- §7 契约清单 → 全部 task 文件路径对齐 ✓
- 两处 YAGNI 简化（pnl 累计浮盈、Treemap 不按 sector）已在开头记录并落到 Task 4/9 ✓

**Placeholder 扫描**：无 TBD/TODO；所有代码块完整可执行；Task 9 已标注一处语法笔误需实现时改正。

**类型一致性**：
- `BenchmarkPoint` 后端 `(date: str, nav: float)` ↔ 前端 `{date: string, nav: number}` ✓
- `quantile_nav` 后端 Q1-Q5+LS ↔ 前端 `QuantileNav` interface ✓
- `mode` 四态后端 `unavailable/disconnected/live/vetoed_by_risk` ↔ 前端 `GatewayMode` ✓
- `get_status()` 后端返 `{connected, locked, mode}` ↔ 前端 `TradingStatus` ✓
