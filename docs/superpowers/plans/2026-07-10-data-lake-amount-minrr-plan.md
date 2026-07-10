# data_lake 接入 + amount 单位统一 + min_rr_ratio 定标 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 打通蔡森流水线「真实数据→扫描→定标」闭环：_load_price_data 接 DataLakeReader（全市场）+ amount 装配转元 + calibrate_min_rr 脚本定标改 config 默认。

**Architecture:** `_load_price_data` 从占位返空改为接 `DataLakeReader.get_timeseries` 按 universe 装配（None/空→全市场枚举），装配时 `amount×1000`（千元→元）统一流动性口径；新增 `DataLakeReader.symbols()` 公开方法封装全市场枚举；新增 `scripts/calibrate_min_rr.py` 跑近 3 年全市场 replay 产出 min_rr 建议值，据以改 `config.py` 默认。

**Tech Stack:** Python 3.10（`.venv310`）、pandas、DataLakeReader（多湖内存缓存）、pytest、tushare 前复权日线 parquet。

## Global Constraints

- Python 3.10，venv `.venv310`（`.venv310/Scripts/python.exe -m pytest`）。
- 全中文注释（CLAUDE.md 强制），注释说明 What + Why。
- amount 单位统一：data_lake `amount`=千元（tushare `pro.daily` 原生），装配时 `×1000` 转元；`risk.liquidity_min_amount=1e8`(元) 不动。
- 真实数据：`data_lake/a_shares_daily.parquet`（MultiIndex(date,symbol)，列 open/high/low/close/volume/amount，2016 起）。
- universe 语义：`None` 或 `[]` → 全市场（reader.symbols 枚举）。
- 不改 risk 阈值、不改 reader 既有 API 语义（只新增 symbols()）。
- 每任务独立 commit，commit message 中文，结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 责任 | 涉及 Task |
|---|---|---|
| `data/lake_reader.py` | 新增 `symbols(lake)` 公开方法（全市场枚举，封装私有 `_lakes`） | Task 1 |
| `server/services/caisen_service.py` | `_load_price_data` 接 reader + amount 转元；`run_scan` 去空 universe 早返；`run_replay` 改传 `req.end` | Task 2, 3 |
| `scripts/calibrate_min_rr.py`（新建） | 跑 replay 定标，打印报告 + min_rr 建议 | Task 4 |
| `caisen/config.py` | `min_rr_ratio` 默认按定标结果改 | Task 5 |
| `tests/test_data_lake_access.py`（新建） | reader.symbols + _load_price_data 接入测试 | Task 1, 2 |

---

## Task 1: DataLakeReader.symbols() 公开方法

**Files:**
- Modify: `data/lake_reader.py`（在 `lakes()` 方法后新增 `symbols()`）
- Test: `tests/test_data_lake_access.py`（新建）

**Interfaces:**
- Produces: `DataLakeReader.symbols(lake: str | None = None) -> list[str]`——返回指定湖（缺省 default_lake）的全部唯一 symbol 列表；无湖/湖不存在返 `[]`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_data_lake_access.py
# -*- coding: utf-8 -*-
"""data_lake 接入测试：DataLakeReader.symbols() 全市场枚举。"""
import pandas as pd
import pytest

from data.lake_reader import DataLakeReader


def _make_reader_with_daily(tmp_path, monkeypatch) -> DataLakeReader:
    """构造一个已 load 小样本 daily 湖的 reader（不污染全局单例）。

    小样本 MultiIndex(date,symbol)，3 个 symbol × 2 日，amount 故意用小值（千元口径）
    便于后续 _load_price_data 测试验证 ×1000 转元。
    """
    df = pd.DataFrame(
        {"open": [10, 11, 20, 21, 30, 31],
         "high": [11, 12, 22, 23, 33, 34],
         "low": [9, 10, 18, 19, 27, 28],
         "close": [10.5, 11.5, 21, 22, 31, 32],
         "volume": [1000, 1100, 2000, 2100, 3000, 3100],
         "amount": [100.0, 110.0, 200.0, 210.0, 300.0, 310.0]},   # 千元口径
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2024-01-02"), "000001.SZ"),
             (pd.Timestamp("2024-01-03"), "000001.SZ"),
             (pd.Timestamp("2024-01-02"), "600000.SH"),
             (pd.Timestamp("2024-01-03"), "600000.SH"),
             (pd.Timestamp("2024-01-02"), "920982.BJ"),
             (pd.Timestamp("2024-01-03"), "920982.BJ")],
            names=["date", "symbol"],
        ),
    )
    path = tmp_path / "daily_sample.parquet"
    df.to_parquet(path)
    reader = DataLakeReader()
    reader.load(str(path), key="daily")
    return reader


def test_symbols_returns_all_unique_symbols(tmp_path, monkeypatch):
    """symbols() 返回 daily 湖全部唯一 symbol（封装 _lakes 私有，全市场枚举入口）。"""
    reader = _make_reader_with_daily(tmp_path, monkeypatch)
    syms = reader.symbols()
    assert set(syms) == {"000001.SZ", "600000.SH", "920982.BJ"}
    assert len(syms) == 3


def test_symbols_empty_when_no_lake_loaded():
    """无任何湖 load 时 symbols() 返空列表（离线降级，不抛）。"""
    reader = DataLakeReader()   # 全新实例，未 load
    assert reader.symbols() == []


def test_symbols_respects_lake_arg(tmp_path, monkeypatch):
    """symbols(lake=X) 仅返回指定湖的 symbol。"""
    reader = _make_reader_with_daily(tmp_path, monkeypatch)
    # daily 湖有 3 个 symbol
    assert len(reader.symbols("daily")) == 3
    # 不存在的湖返空
    assert reader.symbols("nonexistent") == []
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_data_lake_access.py -v`
Expected: 3 FAIL（`AttributeError: 'DataLakeReader' object has no attribute 'symbols'`）

- [ ] **Step 3: 实现 symbols()**

在 `data/lake_reader.py` 的 `lakes()` 方法（约 line 69-71）后新增：

```python
    def symbols(self, lake: str | None = None) -> list[str]:
        """返回指定湖（缺省 default_lake）的全部唯一 symbol 列表。

        用途：caisen_service._load_price_data 全市场枚举入口（universe=None/空 时调用），
        封装私有 _lakes 避免调用方穿透。

        无湖或指定湖不存在 → 返空列表（离线降级，不抛异常，与 get_* 同口径）。
        """
        key = self._resolve(lake)
        if key is None or key not in self._lakes:
            return []
        return list(self._lakes[key].index.get_level_values("symbol").unique())
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_data_lake_access.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add data/lake_reader.py tests/test_data_lake_access.py
git commit -m "feat(data): DataLakeReader.symbols() 全市场枚举公开方法"
```

> 注：`data/` 在 .gitignore，`git add` 会触发忽略告警但已跟踪文件（lake_reader.py）正常暂存。

---

## Task 2: _load_price_data 接 reader + amount 转元

**Files:**
- Modify: `server/services/caisen_service.py:71-94`（`_load_price_data` 重写）
- Test: `tests/test_data_lake_access.py`（追加）

**Interfaces:**
- Consumes: `DataLakeReader.get_instance()`、`.loaded`、`.symbols(lake)`、`.get_timeseries(symbol,start,end,lake)`；`config.LAKE_CONFIG["default_lake"]`。
- Produces: `_load_price_data(symbols, date)` 返 `{symbol: DataFrame}`，amount 已转元；reader 离线/空时返 `{}`。

- [ ] **Step 1: 写失败测试**（追加到 tests/test_data_lake_access.py）

```python
def test_load_price_data_assembles_and_converts_amount(tmp_path, monkeypatch):
    """_load_price_data 接 reader：装配 {symbol:df} + amount×1000（千元→元）。"""
    from server.services import caisen_service as svc
    reader = _make_reader_with_daily(tmp_path, monkeypatch)
    monkeypatch.setattr("data.lake_reader.DataLakeReader.get_instance",
                        classmethod(lambda cls: reader))

    # date 取湖内某日（截到该日）
    pd_data = svc._load_price_data(["000001.SZ"], "2024-01-03")

    assert "000001.SZ" in pd_data
    df = pd_data["000001.SZ"]
    # amount 已 ×1000 转元（原 110.0 千元 → 110000.0 元）
    assert df["amount"].iloc[-1] == pytest.approx(110000.0, rel=1e-9)
    # OHLCV 列齐全
    for c in ("open", "high", "low", "close", "volume", "amount"):
        assert c in df.columns


def test_load_price_data_full_market_when_symbols_empty(tmp_path, monkeypatch):
    """symbols=None/[] → 全市场枚举（reader.symbols）。"""
    from server.services import caisen_service as svc
    reader = _make_reader_with_daily(tmp_path, monkeypatch)
    monkeypatch.setattr("data.lake_reader.DataLakeReader.get_instance",
                        classmethod(lambda cls: reader))

    pd_data = svc._load_price_data(None, "2024-01-03")   # None → 全市场
    assert set(pd_data.keys()) == {"000001.SZ", "600000.SH", "920982.BJ"}

    pd_data2 = svc._load_price_data([], "2024-01-03")    # 空列表 → 全市场
    assert set(pd_data2.keys()) == {"000001.SZ", "600000.SH", "920982.BJ"}


def test_load_price_data_empty_when_reader_offline(monkeypatch):
    """reader 未 load（离线/CI）→ 返空 dict（降级，不抛）。"""
    from server.services import caisen_service as svc
    offline = type("R", (), {"loaded": False, "symbols": lambda self, l=None: []})()
    monkeypatch.setattr("data.lake_reader.DataLakeReader.get_instance",
                        classmethod(lambda cls: offline))

    assert svc._load_price_data(None, "2024-01-03") == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_data_lake_access.py -k load_price_data -v`
Expected: 3 FAIL（当前 _load_price_data 占位返空 {}，断言失败）

- [ ] **Step 3: 重写 _load_price_data**

替换 `server/services/caisen_service.py:71-94`（整个 `_load_price_data` 函数体）：

```python
def _load_price_data(symbols: Optional[List[str]], date: str) -> Dict[str, pd.DataFrame]:
    """按标的池 + 日期从 data_lake 装配 price_data（生产走 DataLakeReader）。

    物理意图：
        生产：DataLakeReader.get_instance()（lifespan 已 load daily 湖进内存），
              按 symbols 逐标的 get_timeseries 取时序并装配。
        universe 语义：symbols 为 None 或 [] → 全市场枚举（reader.symbols）。
        单位统一：data_lake amount 为千元（tushare pro.daily 原生），装配时 ×1000 转元，
              与 risk.liquidity_min_amount=1e8(元) 口径一致（#3）。
        离线降级：reader 未 load / 湖空 / 全部 symbol 取空 → 返 {}，run_scan/run_replay
              按既有契约降级（空候选 / 零统计），不抛异常。

    参数：
        symbols: 标的池（ScanRequest.universe / ReplayRequest.universe）。
                 None 或 [] → 全市场。
        date:    截止交易日（scan 传 T 日取 [:T] 无前视；replay 传 req.end 取全历史段）。

    返回：
        {symbol: DataFrame}（OHLCV + amount 已转元）。离线/空时返回 {}。
    """
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG

    reader = DataLakeReader.get_instance()
    if not reader.loaded:
        logger.debug("_load_price_data 离线（reader 未 load），返空 dict")
        return {}
    lake = LAKE_CONFIG.get("default_lake") or "daily"

    # universe 解析：None/空 → 全市场枚举
    if not symbols:
        symbols = reader.symbols(lake)
        if not symbols:
            logger.debug("_load_price_data 湖无 symbol（lake=%s），返空 dict", lake)
            return {}

    price_data: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        # start 用足够早的固定日期（早于 daily 湖起点 2016），get_timeseries 的
        # .loc[start:end] 闭区间切片自然截到实际数据范围；end=date 截到 T 日（无前视）。
        ts = reader.get_timeseries(sym, start="2010-01-01", end=date, lake=lake)
        if ts is None or ts.empty:
            continue
        # 列对齐（screener 需 close/high/low/volume/amount）
        cols = [c for c in ("open", "high", "low", "close", "volume", "amount")
                if c in ts.columns]
        ts = ts[cols].copy()
        # #3 单位统一：amount 千元 → 元（流动性过滤口径统一；volume 手单位不影响策略，
        # 放量校验全用比例 right_vol_shrink/breakout_vol_multiplier，单位无关）。
        if "amount" in ts.columns:
            ts["amount"] = ts["amount"] * 1000.0
        price_data[sym] = ts
    return price_data
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_data_lake_access.py -v`
Expected: 全部 PASS（Task1 的 3 + Task2 的 3 = 6）

- [ ] **Step 5: Commit**

```bash
git add server/services/caisen_service.py tests/test_data_lake_access.py
git commit -m "feat(caisen): _load_price_data 接 DataLakeReader + amount 千元→元"
```

---

## Task 3: run_scan 去空 universe 早返 + run_replay 改传 req.end

**Files:**
- Modify: `server/services/caisen_service.py:204-205`（run_scan 去早返）、`:387`（run_replay 传 req.end）
- Test: `tests/test_caisen_service.py`（订正/新增）

**背景**：
- `run_scan:204-205` 的 `if not req.universe: return []` 会拦截空 universe → scan_universe beat（传 `universe=[]`）永远早返、不触发全市场扫描。去掉早返，让空 universe 流向 _load_price_data 全市场枚举。
- `run_replay:387` 传 `date=req.start`（取 `[:start]`）数据不足；replay 需 `[start,end]` 全段，应传 `req.end`。

**Interfaces:** 无新接口；调整 run_scan/run_replay 对 _load_price_data 的传参。

- [ ] **Step 1: 写失败测试**（追加到 tests/test_caisen_service.py）

```python
def test_run_scan_empty_universe_flows_to_full_market(monkeypatch):
    """【Task3】空 universe 不再早返，流向 _load_price_data 全市场枚举。

    scan_universe beat 传 universe=[] → run_scan 应调 _load_price_data（而非早返 []）。
    monkeypatch _load_price_data 返空（模拟离线），run_scan 降级返 []；关键断言是
    _load_price_data 被调用（证明未早返）。
    """
    from server.services import caisen_service as svc
    called = {}
    def _fake_load(symbols, date):
        called["symbols"] = symbols
        called["invoked"] = True
        return {}   # 模拟离线/空，run_scan 降级返 []
    monkeypatch.setattr(svc, "_load_price_data", _fake_load)
    req = ScanRequest(date="2024-01-15", universe=[], cfg_override=dict(_LOOSE_CFG_OVERRIDE))
    plans = svc.run_scan(req)
    assert plans == []                      # 离线降级返空
    assert called.get("invoked") is True, "空 universe 应流向 _load_price_data（不再早返）"
    assert called.get("symbols") == []      # 空 universe 原样透传给 _load_price_data
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_caisen_service.py::test_run_scan_empty_universe_flows_to_full_market -v`
Expected: FAIL（当前 run_scan 早返，_load_price_data 未被调用）

- [ ] **Step 3: 改 run_scan（去早返）**

`server/services/caisen_service.py` run_scan 中，删除这两行（约 204-205）：

```python
    if not req.universe:
        return []
```

并在其位置加注释说明（紧接 `try:` 之前）：

```python
    # 【Task3】空 universe 不再早返：流向 _load_price_data 全市场枚举（scan_universe beat
    # 传 universe=[] 触发全市场扫描；reader 离线时 _load_price_data 返空 → 下面 candidates
    # 为空 → 降级返 []）。
```

- [ ] **Step 4: 改 run_replay（传 req.end）**

`server/services/caisen_service.py:387`：

```python
        price_data = _load_price_data(universe, req.start)
```

改为：

```python
        # 【Task3】传 req.end（取 [start,end] 全段），而非 req.start（[:start] 数据不足）；
        # backtest_replay.replay 内部按 T∈[start,end] 滚动 .loc[:T] 隔离未来（无前视）。
        price_data = _load_price_data(universe, req.end)
```

- [ ] **Step 5: 运行确认通过 + 既有回归**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_caisen_service.py -v`
Expected: 全 PASS（含新测试 + 既有 run_scan/run_replay 测试，它们用 monkeypatch 注入合成 price_data，不受 _load_price_data 改动影响）

- [ ] **Step 6: Commit**

```bash
git add server/services/caisen_service.py tests/test_caisen_service.py
git commit -m "fix(caisen): run_scan 空 universe 流向全市场 + run_replay 传 req.end 取全段"
```

---

## Task 4: calibrate_min_rr.py 定标脚本

**Files:**
- Create: `scripts/calibrate_min_rr.py`
- 验证：手动小样本运行（`--sample 5 --years 1`）

**Interfaces:**
- Consumes: `caisen.backtest_replay.replay`、`caisen.config.StrategyConfig`、`caisen.risk.RiskManager`、`caisen_service._load_price_data`（全市场枚举 + amount 转元）。
- Produces: 打印 ReplayReport（n_hits/win_rate/avg_rr/max_drawdown/min_rr_ratio_recommendation）+ rr 分布。

- [ ] **Step 1: 创建脚本**

```python
# scripts/calibrate_min_rr.py
# -*- coding: utf-8 -*-
"""min_rr_ratio 数据驱动定标：跑近 N 年全市场 replay → 产出建议值。

物理意图（Phase3+ 待办⑤）：
    Phase 2 Bug4 修后标准 W 底新公式 rr≈1.4 < 生产默认 min_rr_ratio=3.0，发不出计划。
    本脚本跑真实历史数据 replay，据胜率/平均盈亏比（_recommend_min_rr）给出数据驱动的
    生产 min_rr_ratio 建议值，人工据以改 caisen/config.py 默认。

用法：
    python scripts/calibrate_min_rr.py                # 默认近 3 年全市场（耗时几十分钟~几小时）
    python scripts/calibrate_min_rr.py --years 1      # 近 1 年
    python scripts/calibrate_min_rr.py --sample 300   # 近 3 年随机采样 300 标的（快速）

输出：n_hits/胜率/平均盈亏比/最大回撤/min_rr_ratio 建议 + rr 分布直方图文本。
"""
from __future__ import annotations
import os
import sys
import argparse
from datetime import datetime, timedelta

# 加项目根到 sys.path（脚本可从任意 cwd 直接运行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen import backtest_replay
from data.lake_reader import DataLakeReader
from config import LAKE_CONFIG
from server.services.caisen_service import _load_price_data


def main(years: int, sample: int | None) -> None:
    # 1. 确保 daily 湖已 load 进 reader（脚本独立运行，不经过 server lifespan）
    reader = DataLakeReader.get_instance()
    daily_path = LAKE_CONFIG["lakes"]["daily"]
    if not reader.loaded or "daily" not in reader.lakes():
        print(f"加载 daily 湖：{daily_path} ...")
        reader.load(daily_path, key="daily")
    if not reader.loaded:
        print("ERROR：daily 湖未加载（parquet 缺失？），定标中止。")
        return

    # 2. 确定回放区间（近 N 年）+ universe
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    all_symbols = reader.symbols("daily")
    universe = all_symbols
    if sample and sample < len(all_symbols):
        universe = list(pd.Series(all_symbols).sample(n=sample, random_state=42))
    print(f"定标区间 {start} ~ {end}，标的数 {len(universe)}（全市场 {len(all_symbols)}）")

    # 3. 装配 price_data（_load_price_data 自动 amount 转元 + 全历史段）
    price_data = _load_price_data(universe, end)
    if not price_data:
        print("ERROR：price_data 装配为空，定标中止。")
        return
    print(f"装配 price_data：{len(price_data)} 标的")

    # 4. 跑 replay（用宽松 min_rr_ratio=0 收集尽可能多命中，统计真实 rr 分布）
    cfg = StrategyConfig(min_rr_ratio=0.0)
    risk = RiskManager(cfg)
    report = backtest_replay.replay(
        price_data, cfg, risk, start=start, end=end, aum=1_000_000.0,
    )

    # 5. 打印报告
    print("\n========== 定标报告 ==========")
    print(f"命中笔数 n_hits     : {report.n_hits}")
    print(f"胜率 win_rate       : {report.win_rate:.1%}")
    print(f"平均盈亏比 avg_rr   : {report.avg_rr:.3f}")
    print(f"最大回撤 max_dd     : {report.max_drawdown:.3f}")
    print(f"平均持仓天数        : {report.avg_holding_bars:.1f}")
    print(f"形态分布            : {report.pattern_dist}")
    print(f"\nmin_rr_ratio 建议   : {report.min_rr_ratio_recommendation}")

    # 6. rr 分布直方图（文本）
    hits = report.metadata.get("hits", [])
    if hits:
        rrs = pd.Series([h["rr"] for h in hits])
        print(f"\nrr 分布（n={len(rrs)}）：mean={rrs.mean():.3f} median={rrs.median():.3f} "
              f"std={rrs.std():.3f}")
        print(f"rr 分位：10%={rrs.quantile(0.1):.3f} 50%={rrs.quantile(0.5):.3f} "
              f"90%={rrs.quantile(0.9):.3f}")
        # 各阈值下的命中率（辅助选 min_rr_ratio）
        for thr in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
            pct = (rrs >= thr).mean()
            print(f"  rr >= {thr} : {pct:.1%} 命中保留 ({(rrs>=thr).sum()}/{len(rrs)})")
    print("==============================\n")
    print("据上述建议值，改 caisen/config.py 的 min_rr_ratio 默认（3.0 → 建议值）。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="min_rr_ratio 数据驱动定标（近 N 年全市场 replay）")
    ap.add_argument("--years", type=int, default=3, help="回放年数（默认 3）")
    ap.add_argument("--sample", type=int, default=None, help="随机采样标的数（缺省全市场）")
    args = ap.parse_args()
    main(years=args.years, sample=args.sample)
```

- [ ] **Step 2: 小样本冒烟运行（验证脚本可跑通）**

Run: `.venv310/Scripts/python.exe scripts/calibrate_min_rr.py --sample 5 --years 1`
Expected: 加载 daily 湖 → 装配 5 标的 → 跑 replay → 打印报告（n_hits 可能为 0~若干，但脚本不报错；min_rr_ratio 建议非空字符串）

- [ ] **Step 3: Commit**

```bash
git add scripts/calibrate_min_rr.py
git commit -m "feat(scripts): calibrate_min_rr.py 近N年全市场 replay 定标脚本"
```

> 注：`--sample 5 --years 1` 命中可能很少（小样本），仅供冒烟。真实定标在 Task 5 用全市场近 3 年跑。

---

## Task 5: 跑定标 → 改 config min_rr_ratio 默认

**Files:**
- Modify: `caisen/config.py:100-103`（min_rr_ratio 默认 + description）

**说明**：本任务为「运行 + 据结果改值」，非 TDD。先跑全市场定标拿建议值，再改 config。

- [ ] **Step 1: 跑近 3 年全市场定标**

Run: `.venv310/Scripts/python.exe scripts/calibrate_min_rr.py --years 3`
Expected: 几十分钟~几小时后输出定标报告（n_hits/胜率/avg_rr/min_rr_ratio 建议 + rr 分位/各阈值命中率）。

- [ ] **Step 2: 据建议值改 config 默认**

读 Step 1 输出的 `min_rr_ratio 建议` + rr 分布。据建议值（如「建议适度放宽阈值…可下调至 2.0」或 EV 计算），选一个使「标准 W 底 rr≈1.4 能通过 + 过滤掉明显劣质（rr<1）」的阈值（候选 1.0~1.5，结合命中率分布定）。

修改 `caisen/config.py` 的 `min_rr_ratio` 字段（约 line 100-103），把默认值 + description 改为定标依据。例（值 X 据定标结果填）：

```python
    min_rr_ratio: float = Field(
        X.X,   # 【定标 2026-07-10】近3年全市场 replay（n_hits=NNN, 胜率=WW%, avg_rr=A.AA），
               # EV=胜率×avg_rr-(1-胜率)；标准 W 底新公式 rr≈1.4，旧默认 3.0 全拦。
               # 据 _recommend_min_rr 建议下调至 X.X（rr>=X.X 命中保留率约 YY%）。
        description="盈亏比下限(回踩均价入场+第n波目标公式，Phase2 Bug4 修后)；"
                    "默认值经近3年全市场 replay 定标（详见 2026-07-10 定标 commit）。",
    )
```

- [ ] **Step 3: 验证标准 W 底能通过 rr 过滤**

写一次性检查（不入库）：用标准 W 底合成序列跑 run_scan（min_rr_ratio 用新默认），确认产出非空计划。或直接跑：

Run: `.venv310/Scripts/python.exe -m pytest tests/caisen/test_plan.py -v`
Expected: PASS（test_plan 的 rr 测试已订正为新公式；新默认值若 ≤ 测试用的 1.0/1.5，不影响）

- [ ] **Step 4: Commit**

```bash
git add caisen/config.py
git commit -m "fix(caisen): min_rr_ratio 默认按近3年全市场定标改为 X.X（Phase3+ 待办⑤）"
```
（commit message 里填实际定标数据：n_hits/胜率/avg_rr/建议依据）

---

## Task 6: 全量验证 + chart 端点回归

**Files:** 无改动（验证性任务）

- [ ] **Step 1: 全量 pytest**

Run: `.venv310/Scripts/python.exe -m pytest -q`
Expected: 全 PASS（既有测试 + 本次新增 test_data_lake_access.py）

- [ ] **Step 2: chart 端点回归确认**

`server/api/v1/caisen.py:220` 的 chart 端点调 `_load_price_data([symbol], formed_at)` 取真实 K 线画图。Task 2 接入后自动获益（能读真实 symbol 时序）。确认既有 chart 测试不回归：

Run: `.venv310/Scripts/python.exe -m pytest tests/test_caisen_api.py -v`
Expected: PASS

- [ ] **Step 3: 端到端冒烟（真实数据全市场扫描）**

Run: `.venv310/Scripts/python.exe -c "
from server.services import caisen_service as svc
from server.schemas.caisen import ScanRequest
req = ScanRequest(date='2026-07-09', universe=[], cfg_override={'min_rr_ratio': 1.0, 'ma26w_filter': False, 'abc_wave_detect': False})
plans = svc.run_scan(req)
print(f'真实全市场扫描产出 {len(plans)} 个候选计划')
if plans:
    p = plans[0]
    print(f'示例：{p.symbol} entry={p.entry_upper} stop={p.stop_loss} tp={p.take_profit} rr={p.rr_ratio:.2f}')
"`
Expected: 加载 daily 湖 → 全市场扫描 → 产出若干候选计划（流动性过滤有命中，单位正确，rr 合理）。若 0 候选，检查流动性阈值/形态参数（真实数据量级）。

---

## Self-Review

**1. Spec 覆盖**：
- #1 data_lake 接入 → Task 1（symbols）+ Task 2（_load_price_data 接 reader）+ Task 3（run_scan 去早返让全市场生效）。✅
- #3 amount 单位统一 → Task 2（装配 ×1000 转元）。✅
- #4 min_rr 定标 → Task 4（脚本）+ Task 5（跑定标改 config）。✅
- 性能策略 → Task 4 脚本支持 `--sample`/`--years`。✅
- 错误处理（离线降级）→ Task 2 离线返空。✅

**2. 占位符扫描**：Task 5 Step 2 的 `X.X` 是「据定标结果填值」（定标本就要跑才知道），非占位 TBD——执行时据 Step 1 输出填实际值。其余步骤代码完整。✅

**3. 类型一致**：`symbols(lake)` / `_load_price_data(symbols, date)` / `get_timeseries(symbol,start,end,lake)` 签名跨任务一致。✅

**4. 未覆盖项**：chart 端点（caisen.py:220）自动获益，Task 6 Step 2 回归确认。✅
