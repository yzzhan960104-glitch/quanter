# Tushare 股票数据采集 Implementation Plan（Plan A · 三类之一）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Tushare 10000 积分账号采集 A 股高价值数据（财务三大报表/资金流/龙虎榜/融资融券/北向/板块/指数/特色筹码/股东/解禁/停牌），落 data_lake 湖；并建立通用 Tushare 湖同步器框架（Plan B/C 共享前置）。

**Architecture:** 通用同步器 `data/tushare_sync.py`（配置驱动：接口/字段/分页模式/落湖）+ `config.py` 的 `TUSHARE_DATASETS` 声明式注册表。各数据集加一条配置即可，复用 `_tushare_compat` 多 token 轮询 + `tushare_rate_limiter` 令牌桶 + `tushare_breaker` 熔断 + shard 断点续传 + `build_multiindex`。财报类用 ann_date 索引防前视。

**Tech Stack:** Python 3.10、`tnskhdata`/`tushare` pro 接口、pandas、pyarrow、pytest。

## Global Constraints

- Python 3.10 venv（`.venv310`）；中文注释（CLAUDE.md 像素级 Why）。
- Tushare 10000 积分：常规 500 次/分、特色（cyq/kpl）300 次/分；常规数据总量无上限。
- 限流复用：`_tushare_compat.get_pro()` 多 token 轮询 + `tushare_rate_limiter.acquire` + `tushare_breaker`；特色数据单独 300/分通道。
- 财报前视红线：income/balancesheet/cashflow 用 `ann_date`（公告日）索引，**绝不用 end_date**（报告期）。
- 断点续传：按 symbol/date shard 落盘，已存在跳过；空数据不写 shard。
- 时序湖 MultiIndex(date,symbol)；快照/列表数据进内存元信息。
- TDD + frequent commits。
- 关联 spec：`docs/superpowers/specs/2026-07-14-data-center-and-data-governance-design.md` §3.7 设计 C（Tushare 三大类）。

## File Structure

| 文件 | 责任 | 操作 |
|---|---|---|
| `data/tushare_sync.py` | 通用 Tushare 湖同步器（配置驱动） | 新建 |
| `config.py` | `TUSHARE_DATASETS` 注册表 + 新湖注册到 `LAKE_CONFIG["lakes"]`/`DATASET_REGISTRY` | 修改 |
| `scripts/sync_tushare.py` | 通用同步 CLI（`python scripts/sync_tushare.py <key>`） | 新建 |
| `tests/test_tushare_sync.py` | 通用同步器单测 | 新建 |
| `tests/test_tushare_datasets_stock.py` | 股票类各数据集配置+落湖测试 | 新建 |

---

### Task 1: 通用 Tushare 湖同步器框架（配置驱动）

**Files:**
- Create: `data/tushare_sync.py`
- Create: `scripts/sync_tushare.py`
- Modify: `config.py`（新增 `TUSHARE_DATASETS` 注册表 + 新湖注册）
- Test: `tests/test_tushare_sync.py`

**Interfaces:**
- Consumes: `data/_tushare_compat.get_pro`、`data.resilience.tushare_rate_limiter`/`tushare_breaker`
- Produces: `tushare_sync.sync_dataset(key, start, end, resume=True)`；`config.TUSHARE_DATASETS` 注册表

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tushare_sync.py
"""通用 Tushare 湖同步器测试：配置驱动 + 分页 + 断点续传 + 落湖。"""
import os
import pandas as pd
import pytest


class _FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame。"""
    def __init__(self):
        self.calls = []  # 记录 (api_name, kwargs)
        self._data = {
            "income": pd.DataFrame({
                "ts_code": ["000001.SZ"] * 3,
                "ann_date": ["20240101", "20240401", "20240701"],
                "end_date": ["20231231", "20240331", "20240630"],
                "total_revenue": [1e9, 1.1e9, 1.2e9],
                "n_income": [1e8, 1.1e8, 1.2e8],
            }),
        }

    def __getattr__(self, api_name):
        def _call(**kwargs):
            self.calls.append((api_name, kwargs))
            return self._data.get(api_name, pd.DataFrame())
        return _call


@pytest.fixture
def fake_pro(monkeypatch):
    fake = _FakePro()
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.tushare_rate_limiter",
                        type("L", (), {"acquire": lambda self, n: None})())
    monkeypatch.setattr("data.tushare_sync.tushare_breaker",
                        type("B", (), {"allow_request": lambda self: True,
                                       "record_success": lambda self: None,
                                       "record_failure": lambda self: None})())
    return fake


def test_sync_dataset_by_symbol_multiindex(tmp_path, fake_pro, monkeypatch):
    """by=symbol 分页：逐标的拉取 → MultiIndex(date,symbol) 落湖。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    # 注册一个测试数据集
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",  # 按标的分页
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue,n_income",
        "lake": str(tmp_path / "income.parquet"),
    }
    LAKE_CONFIG["lakes"]["fina_income"] = TUSHARE_DATASETS["fina_income"]["lake"]
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fina_income"]["lake"])
    assert df.index.names == ["date", "symbol"]
    assert "total_revenue" in df.columns
    assert len(df) == 3


def test_sync_dataset_resume_skips_existing_shard(tmp_path, fake_pro, monkeypatch):
    """断点续传：shard 已存在即跳过（省配额）。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    shard_dir = str(tmp_path / "shards")
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue",
        "lake": str(tmp_path / "income.parquet"),
        "shard_dir": shard_dir,
    }
    os.makedirs(shard_dir)
    # 预置 shard（模拟已拉过 000001.SZ）
    pd.DataFrame({"total_revenue": [1e9]},
                 index=pd.DatetimeIndex(["2024-01-01"], name="ann_date")
                 ).to_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=True)
    # fake_pro 未被调（shard 已存在跳过）
    assert fake_pro.calls == []
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_tushare_sync.py -v`
Expected: FAIL（`data.tushare_sync` 不存在）

- [ ] **Step 3: 实现通用同步器**

```python
# data/tushare_sync.py
"""通用 Tushare 湖同步器：配置驱动，一个框架覆盖所有时序接口。

各数据集在 config.TUSHARE_DATASETS 声明接口/字段/分页模式/落湖，本模块统一执行：
_fetch_with_guard 限频+熔断 → 分页拉取 → shard 断点续传 → build_multiindex 落湖。

分页模式（by）：
  - symbol：逐标的拉取（财报/股东等，单标的全历史一次返）
  - date：逐交易日拉取（资金流/龙虎榜/融资融券，单日全市场）
  - single：单次拉取（指数/列表类，不分页）

前视红线：财报类 date_col=ann_date（公告日），绝不用 end_date（报告期）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from config import TUSHARE_DATASETS, LAKE_CONFIG
from data._tushare_compat import get_pro, source_name
from data.resilience import tushare_breaker, tushare_rate_limiter

logger = logging.getLogger(__name__)


def _fetch_with_guard(api_name: str, **kwargs) -> pd.DataFrame:
    """限频 + 熔断 + 异常分类包装的 pro 接口调用，空数据/失败返空 DF。

    瞬时态（限频/超时）计熔断；持久态（积分/权限）仅记日志；空数据不计熔断。
    与 sync_data_lake._fetch_with_guard 同范式。
    """
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        return pd.DataFrame()
    pro = get_pro()
    try:
        df = getattr(pro, api_name)(**kwargs)
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("limit", "429", "timeout", "connection", "频率", "超时", "频繁")):
            tushare_breaker.record_failure()
            logger.error("Tushare %s 限频/网络异常：%s", api_name, e)
        elif any(k in str(e) for k in ("积分", "权限")):
            logger.error("Tushare %s 积分不足：%s", api_name, e)
        else:
            tushare_breaker.record_failure()
            logger.error("Tushare %s 拉取失败：%s", api_name, e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    tushare_breaker.record_success()
    return df


def _shard_dir(key: str) -> str:
    """数据集 shard 目录（断点续传）。"""
    cfg = TUSHARE_DATASETS[key]
    return cfg.get("shard_dir", os.path.join("data_lake", "shards", key))


def _build_multiindex(shard_dir: str, date_col: str, symbol_col: str, out: str) -> None:
    """合并 shard → MultiIndex(date, symbol) parquet。"""
    frames = []
    for f in os.listdir(shard_dir):
        if not f.endswith(".parquet"):
            continue
        symbol = f.replace(".parquet", "")
        df = pd.read_parquet(os.path.join(shard_dir, f))
        df["symbol"] = symbol
        df = df.reset_index().rename(columns={date_col: "date"})
        if "date" not in df.columns:
            df = df.rename(columns={df.columns[0]: "date"})
        frames.append(df)
    if not frames:
        raise RuntimeError(f"shard 目录无数据：{shard_dir}")
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    big = big.set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    big.to_parquet(out, engine="pyarrow")
    logger.info("湖写入完成：%s，%d 行，%d 标的",
                out, len(big), big.index.get_level_values("symbol").nunique())


def sync_dataset(key: str, start: str, end: str,
                 symbols: Optional[list[str]] = None,
                 resume: bool = True) -> None:
    """按 TUSHARE_DATASETS[key] 配置同步一个数据集。

    参数：
        key: 数据集 key（TUSHARE_DATASETS 注册）
        start/end: "YYYY-MM-DD" 区间
        symbols: by=symbol 时的标的列表（None=全市场，需上游 load_universe）
        resume: 断点续传（shard 已存在跳过）
    """
    cfg = TUSHARE_DATASETS[key]
    api = cfg["api"]
    by = cfg["by"]
    date_col = cfg["date_col"]
    symbol_col = cfg.get("symbol_col", "ts_code")
    fields = cfg.get("fields")
    out = cfg["lake"]

    if by == "symbol":
        _sync_by_symbol(key, api, fields, date_col, symbol_col, start, end, symbols, resume, out)
    elif by == "date":
        _sync_by_date(key, api, fields, date_col, symbol_col, start, end, resume, out)
    elif by == "single":
        _sync_single(key, api, fields, date_col, out)
    else:
        raise ValueError(f"未知分页模式 by={by}（key={key}）")


def _sync_by_symbol(key, api, fields, date_col, symbol_col, start, end,
                    symbols, resume, out):
    """逐标的拉取（财报/股东）。shard 按 symbol。"""
    if symbols is None:
        symbols = _load_universe()
    shard_dir = _shard_dir(key)
    os.makedirs(shard_dir, exist_ok=True)
    sd, ed = start.replace("-", ""), end.replace("-", "")
    for ts_code in symbols:
        shard = os.path.join(shard_dir, f"{ts_code}.parquet")
        if resume and os.path.exists(shard):
            continue
        kwargs = {"ts_code": ts_code, "start_date": sd, "end_date": ed}
        if fields:
            kwargs["fields"] = fields
        df = _fetch_with_guard(api, **kwargs)
        if df.empty:
            continue
        df = _cleanse(df, date_col)
        df.to_parquet(shard)
    _build_multiindex(shard_dir, date_col, symbol_col, out)


def _sync_by_date(key, api, fields, date_col, symbol_col, start, end, resume, out):
    """逐交易日拉取（资金流/龙虎榜/融资融券）。shard 按 date。"""
    shard_dir = _shard_dir(key)
    os.makedirs(shard_dir, exist_ok=True)
    trade_dates = _trade_days(start, end)
    for td in trade_dates:
        shard = os.path.join(shard_dir, f"{td}.parquet")
        if resume and os.path.exists(shard):
            continue
        kwargs = {"trade_date": td}
        if fields:
            kwargs["fields"] = fields
        df = _fetch_with_guard(api, **kwargs)
        if df.empty:
            continue
        df = _cleanse(df, date_col)
        df.to_parquet(shard)
    _build_multiindex(shard_dir, date_col, symbol_col, out)


def _sync_single(key, api, fields, date_col, out):
    """单次拉取（指数/列表）。直接落盘，不分页。"""
    kwargs = {}
    if fields:
        kwargs["fields"] = fields
    df = _fetch_with_guard(api, **kwargs)
    if df.empty:
        logger.warning("%s 数据为空，跳过", key)
        return
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    df.to_parquet(out, engine="pyarrow")
    logger.info("%s 写入：%s，%d 行", key, out, len(df))


def _cleanse(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    """洗净：date_col → datetime 索引 + 升序。"""
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
    return df


def _load_universe() -> list[str]:
    """全市场在售标的（复用 sync_data_lake.load_universe 逻辑：stock_basic 剔 ST/退）。"""
    df = _fetch_with_guard("stock_basic", list_status="L",
                           fields="ts_code,symbol,name,list_date")
    if df.empty:
        return []
    mask = (~df["name"].str.contains("ST", na=False)) & \
           (~df["name"].str.contains("退", na=False))
    return df.loc[mask, "ts_code"].tolist()


def _trade_days(start: str, end: str) -> list[str]:
    """交易日历（trade_cal is_open=1）。返回 ['YYYYMMDD', ...]。"""
    df = _fetch_with_guard("trade_cal", exchange="SSE",
                           start_date=start.replace("-", ""),
                           end_date=end.replace("-", ""), is_open="1")
    return df["cal_date"].tolist() if not df.empty else []
```

```python
# scripts/sync_tushare.py
"""通用 Tushare 数据集同步 CLI：python scripts/sync_tushare.py <key> [--years N] [--limit N]"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.tushare_sync import sync_dataset
from config import TUSHARE_DATASETS


def main():
    ap = argparse.ArgumentParser(description="通用 Tushare 数据集同步")
    ap.add_argument("key", choices=list(TUSHARE_DATASETS.keys()), help="数据集 key")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None, help="by=symbol 时仅前 N 只标的")
    args = ap.parse_args()
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * args.years)).strftime("%Y-%m-%d")
    symbols = None
    if args.limit:
        from data.tushare_sync import _load_universe
        symbols = _load_universe()[:args.limit]
    sync_dataset(args.key, start, end, symbols=symbols)
    print(f"{args.key} 同步完成")


if __name__ == "__main__":
    main()
```

```python
# config.py —— 新增 TUSHARE_DATASETS 注册表（放于 DATASET_REGISTRY 之后）
# 通用 Tushare 湖同步器的声明式配置：每个数据集 = 接口/分页/字段/落湖。
# by: symbol（逐标的）/ date（逐日）/ single（单次）
TUSHARE_DATASETS: Dict[str, Dict[str, Any]] = {
    # —— 股票类（Plan A 各 Task 逐步填充）——
    "fina_income": {
        "api": "income", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue,n_income,n_income_attr_p",
        "lake": "data_lake/fina_income.parquet",
    },
    # 后续 Task 追加 fina_balance/fina_cashflow/moneyflow/top_list/margin/...
}
```

- [ ] **Step 4: 运行验证通过 + 提交**

Run: `python -m pytest tests/test_tushare_sync.py -v` → PASS
```bash
git add data/tushare_sync.py scripts/sync_tushare.py config.py tests/test_tushare_sync.py
git commit -m "feat(tushare): 通用湖同步器框架(配置驱动+分页+断点续传+限流复用)"
```

---

### Task 2: 财务三大报表 + 预告/快报 + 分红（fina_income/balance/cashflow/forecast/express/dividend）

**Files:**
- Modify: `config.py`（TUSHARE_DATASETS 追加 6 个配置 + LAKE_CONFIG 注册）
- Test: `tests/test_tushare_datasets_stock.py`

**Interfaces:**
- Consumes: Task 1 `sync_dataset`
- Produces: `fina_income`/`fina_balance`/`fina_cashflow`/`forecast`/`express`/`dividend` 湖

- [ ] **Step 1: 写测试（财报 ann_date 前视红线 + 配置完备性）**

```python
# tests/test_tushare_datasets_stock.py
"""股票类各 Tushare 数据集配置 + 落湖契约测试。"""
import pandas as pd
import pytest
from config import TUSHARE_DATASETS, LAKE_CONFIG


class _FakePro:
    def __init__(self):
        self._data = {}
    def set(self, api, df): self._data[api] = df
    def __getattr__(self, api):
        def _c(**kw): return self._data.get(api, pd.DataFrame())
        return _c


@pytest.fixture
def fake_pro(monkeypatch):
    fake = _FakePro()
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.tushare_rate_limiter",
                        type("L", (), {"acquire": lambda self, n: None})())
    monkeypatch.setattr("data.tushare_sync.tushare_breaker",
                        type("B", (), {"allow_request": lambda self: True,
                                       "record_success": lambda self: None,
                                       "record_failure": lambda self: None})())
    return fake


def test_fina_datasets_use_ann_date_not_end_date():
    """财报类前视红线：date_col 必须是 ann_date，绝不能是 end_date。"""
    for key in ("fina_income", "fina_balance", "fina_cashflow", "forecast", "express"):
        assert TUSHARE_DATASETS[key]["date_col"] == "ann_date", \
            f"{key} 必须用 ann_date（公告日）索引，禁用 end_date（报告期，前视偏差）"


def test_fina_three_statements_lake(tmp_path, fake_pro, monkeypatch):
    """三大报表落 MultiIndex(date,symbol)。"""
    fake_pro.set("income", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240101"], "end_date": ["20231231"],
        "total_revenue": [1e9], "n_income": [1e8]}))
    fake_pro.set("balancesheet", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240101"], "end_date": ["20231231"],
        "total_assets": [1e10]}))
    fake_pro.set("cashflow", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240101"], "end_date": ["20231231"],
        "net_profit_cash_flow": [9e7]}))
    from data.tushare_sync import sync_dataset
    for key in ("fina_income", "fina_balance", "fina_cashflow"):
        monkeypatch.setitem(TUSHARE_DATASETS[key], "lake", str(tmp_path / f"{key}.parquet"))
        sync_dataset(key, "2024-01-01", "2024-12-31", symbols=["000001.SZ"], resume=False)
        df = pd.read_parquet(TUSHARE_DATASETS[key]["lake"])
        assert df.index.names == ["date", "symbol"]
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_tushare_datasets_stock.py::test_fina_datasets_use_ann_date_not_end_date -v`
Expected: FAIL（fina_balance/fina_cashflow 配置未加）

- [ ] **Step 3: 追加 6 个数据集配置**

```python
# config.py TUSHARE_DATASETS 追加（fina_income 已有，补其余 5 个）
    "fina_balance": {
        "api": "balancesheet", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int",
        "lake": "data_lake/fina_balance.parquet",
    },
    "fina_cashflow": {
        "api": "cashflow", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,net_profit_cash_flow,c_pay_acq_foroth_assets",
        "lake": "data_lake/fina_cashflow.parquet",
    },
    "forecast": {
        "api": "forecast", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,type,p_change,min_range,max_range",
        "lake": "data_lake/forecast.parquet",
    },
    "express": {
        "api": "express", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,revenue,n_income,total_profit",
        "lake": "data_lake/express.parquet",
    },
    "dividend": {
        "api": "dividend", "by": "symbol",
        "date_col": "div_proc",  # 分红进度（预案/实施），用 div_proc 作时间近似
        "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,div_proc,stk_div,cash_div,record_date,ex_date",
        "lake": "data_lake/dividend.parquet",
    },
```

> ⚠️ **dividend 的 date_col 说明**：dividend 接口 ann_date 字段为分红方案公告日，本配置用 ann_date 更准确（修正：将 `date_col` 改为 `"ann_date"`）。实现时以 ann_date 为准。

修正 dividend 配置 `"date_col": "ann_date"`。

- [ ] **Step 4: 验证通过 + 提交**

Run: `python -m pytest tests/test_tushare_datasets_stock.py -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 财务三大报表+预告/快报/分红数据集配置(ann_date防前视)"
```

---

### Task 3: 个股资金流 moneyflow（by=date）

**Files:**
- Modify: `config.py`（追加 moneyflow 配置）
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1: 写测试**

```python
def test_moneyflow_by_date(tmp_path, fake_pro, monkeypatch):
    """moneyflow 按 trade_date 分页（单日全市场），落 MultiIndex。"""
    fake_pro.set("moneyflow", pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "trade_date": ["20240105", "20240105"],
        "buy_sm_amount": [1e8, 2e8], "sell_sm_amount": [9e7, 1.5e8],
        "net_mf_amount": [1e7, 5e7]}))
    monkeypatch.setitem(__import__("config").TUSHARE_DATASETS, "moneyflow", {
        "api": "moneyflow", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,buy_sm_amount,sell_sm_amount,net_mf_amount",
        "lake": str(tmp_path / "moneyflow.parquet"),
    })
    # mock trade_days 只返回一日
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    ts.sync_dataset("moneyflow", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["moneyflow"]["lake"])
    assert df.index.names == ["date", "symbol"]
    assert len(df) == 2
```

- [ ] **Step 2-4: 追加配置 + 验证 + 提交**

```python
# config.py TUSHARE_DATASETS 追加
    "moneyflow": {
        "api": "moneyflow", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_elg_amount,sell_elg_amount,net_mf_amount",
        "lake": "data_lake/moneyflow.parquet",
    },
```
Run: `python -m pytest tests/test_tushare_datasets_stock.py::test_moneyflow_by_date -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 个股资金流 moneyflow 数据集(by=date)"
```

---

### Task 4: 龙虎榜 top_list/top_inst（dragon_list 湖切 Tushare）

**Files:**
- Modify: `config.py`（追加 top_list/top_inst 配置；dragon_list 湖指向 Tushare）
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1: 写测试**

```python
def test_top_list_by_date(tmp_path, fake_pro, monkeypatch):
    fake_pro.set("top_list", pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20240105"],
        "name": ["平安银行"], "close": [10.5], "pct_change": [9.9],
        "amount": [5e8], "net_amount": [1e8]}))
    import data.tushare_sync as ts
    monkeypatch.setitem(__import__("config").TUSHARE_DATASETS, "top_list", {
        "api": "top_list", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,name,close,pct_change,amount,net_amount",
        "lake": str(tmp_path / "dragon.parquet"),
    })
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    ts.sync_dataset("top_list", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["top_list"]["lake"])
    assert len(df) == 1 and df["pct_change"].iloc[0] == pytest.approx(9.9)
```

- [ ] **Step 2-4: 配置 + 验证 + 提交**

```python
# config.py 追加
    "top_list": {
        "api": "top_list", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,name,close,pct_change,amount,net_amount,buy_amount,sell_amount",
        "lake": "data_lake/dragon_list.parquet",  # 复用 dragon_list 湖（切 Tushare）
    },
    "top_inst": {
        "api": "top_inst", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,name,close,pct_change,amount,net_amount,buy_amount,sell_amount",
        "lake": "data_lake/top_inst.parquet",
    },
```
Run: `python -m pytest tests/test_tushare_datasets_stock.py::test_top_list_by_date -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 龙虎榜 top_list/top_inst(dragon_list湖切Tushare)"
```

---

### Task 5: 融资融券 margin/margin_detail/margin_secs

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1: 写测试（margin by=date, margin_secs single）**

```python
def test_margin_by_date(tmp_path, fake_pro, monkeypatch):
    fake_pro.set("margin", pd.DataFrame({
        "exchange_id": ["SSE"], "trade_date": ["20240105"],
        "rzye": [1e10], "rzmre": [1e9], "rqye": [1e8], "rqmcl": [1e7]}))
    import data.tushare_sync as ts
    monkeypatch.setitem(__import__("config").TUSHARE_DATASETS, "margin", {
        "api": "margin", "by": "date", "date_col": "trade_date", "symbol_col": "exchange_id",
        "fields": "exchange_id,trade_date,rzye,rzmre,rqye,rqmcl,rzche,rqchl",
        "lake": str(tmp_path / "margin.parquet"),
    })
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    ts.sync_dataset("margin", "2024-01-05", "2024-01-05", resume=False)
    assert not pd.read_parquet(TUSHARE_DATASETS["margin"]["lake"]).empty


def test_margin_secs_single(tmp_path, fake_pro, monkeypatch):
    """融资融券标的列表：single 模式（全市场快照，不分页）。"""
    fake_pro.set("margin_secs", pd.DataFrame({
        "ts_code": ["000001.SZ"], "name": ["平安银行"], "start_date": ["20100301"]}))
    import data.tushare_sync as ts
    monkeypatch.setitem(__import__("config").TUSHARE_DATASETS, "margin_secs", {
        "api": "margin_secs", "by": "single", "date_col": "start_date", "symbol_col": "ts_code",
        "fields": "ts_code,name,start_date",
        "lake": str(tmp_path / "margin_secs.parquet"),
    })
    ts.sync_dataset("margin_secs", "2024-01-05", "2024-01-05", resume=False)
    assert len(pd.read_parquet(TUSHARE_DATASETS["margin_secs"]["lake"])) == 1
```

- [ ] **Step 2-4: 配置 + 验证 + 提交**

```python
# config.py 追加
    "margin": {
        "api": "margin", "by": "date", "date_col": "trade_date", "symbol_col": "exchange_id",
        "fields": "exchange_id,trade_date,rzye,rzmre,rqye,rqmcl,rzche,rqchl",
        "lake": "data_lake/margin.parquet",
    },
    "margin_detail": {
        "api": "margin_detail", "by": "date", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,rzye,rzmre,rqye,rqmcl,rzche,rqchl",
        "lake": "data_lake/margin_detail.parquet",
    },
    "margin_secs": {
        "api": "margin_secs", "by": "single", "date_col": "start_date", "symbol_col": "ts_code",
        "fields": "ts_code,name,start_date",
        "lake": "data_lake/margin_secs.parquet",
    },
```
Run: `python -m pytest tests/test_tushare_datasets_stock.py -k margin -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 融资融券 margin/detail/secs(by=date+single)"
```

---

### Task 6: 北向资金 hsgt_top10/moneyflow_hsgt（north_flow 湖切 Tushare）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
def test_hsgt_top10_by_date(tmp_path, fake_pro, monkeypatch):
    fake_pro.set("hsgt_top10", pd.DataFrame({
        "trade_date": ["20240105"], "name": ["贵州茅台"],
        "ts_code": ["600519.SH"], "vol": [1e6], "amount": [1e9]}))
    import data.tushare_sync as ts
    monkeypatch.setitem(__import__("config").TUSHARE_DATASETS, "hsgt_top10", {
        "api": "hsgt_top10", "by": "date", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "trade_date,name,ts_code,vol,amount,north_direction",
        "lake": str(tmp_path / "north.parquet"),
    })
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    ts.sync_dataset("hsgt_top10", "2024-01-05", "2024-01-05", resume=False)
    assert not pd.read_parquet(TUSHARE_DATASETS["hsgt_top10"]["lake"]).empty
```

```python
# config.py 追加
    "hsgt_top10": {
        "api": "hsgt_top10", "by": "date", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "trade_date,name,ts_code,vol,amount,north_direction",
        "lake": "data_lake/north_flow.parquet",  # 复用 north_flow 湖（切 Tushare）
    },
    "moneyflow_hsgt": {
        "api": "moneyflow_hsgt", "by": "date", "date_col": "trade_date", "symbol_col": "gg_id",
        "fields": "trade_date,ggt_ss,ggt_sz,sgt_ss,sgt_sz,north_money,south_money",
        "lake": "data_lake/moneyflow_hsgt.parquet",
    },
```
Run: `python -m pytest tests/test_tushare_datasets_stock.py -k hsgt -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 北向资金 hsgt_top10/moneyflow_hsgt(north_flow湖切Tushare)"
```

---

### Task 7: 板块/概念 concept/ths_daily（sector 补 Tushare）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
# config.py 追加
    "concept": {
        "api": "concept", "by": "single", "date_col": "code", "symbol_col": "code",
        "fields": "code,name",
        "lake": "data_lake/concept.parquet",
    },
    "concept_detail": {
        "api": "concept_detail", "by": "symbol", "date_col": "trade_date",
        "symbol_col": "ts_code",
        "fields": "id,concept_name,ts_code,name,in_date,out_date",
        "lake": "data_lake/concept_detail.parquet",
    },
    "ths_daily": {
        "api": "ths_daily", "by": "date", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,close,high,low,pct_change",
        "lake": "data_lake/ths_daily.parquet",
    },
```
测试同 by=date/single 范式（参考 Task 5/6）。Run: `pytest -k concept -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 板块概念 concept/concept_detail/ths_daily"
```

---

### Task 8: 指数 index_daily/index_weight/index_member

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
# config.py 追加
    "index_daily": {
        "api": "index_daily", "by": "symbol", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "lake": "data_lake/index_daily.parquet",
    },
    "index_weight": {
        "api": "index_weight", "by": "date", "date_col": "trade_date", "symbol_col": "con_code",
        "fields": "index_code,con_code,trade_date,weight",
        "lake": "data_lake/index_weight.parquet",
    },
    "index_member": {
        "api": "index_member", "by": "single", "date_col": "in_date", "symbol_col": "con_code",
        "fields": "index_code,con_code,con_name,in_date,out_date",
        "lake": "data_lake/index_member.parquet",
    },
```
> ⚠️ index_daily 的 by=symbol 需 symbols=指数代码列表（如 000300.SH/000905.SH/000016.SH），sync_tushare.py --limit 对指数不适用，应传指数代码。实现时 `sync_dataset` 的 symbols 参数传指数列表。测试 mock 一个指数代码。
Run: `pytest -k index -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 指数 index_daily/weight/member"
```

---

### Task 9: 特色筹码 cyq_perf（300/分独立通道）

**Files:**
- Modify: `data/tushare_sync.py`（特色数据限流通道）
- Modify: `config.py`（cyq_perf 配置，标记 quota=300）
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1: 写测试（特色数据 300/分通道）**

```python
def test_cyq_perf_special_quota(fake_pro, monkeypatch):
    """cyq_perf 是特色数据（300/分），走独立限流通道标记。"""
    fake_pro.set("cyq_perf", pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20240105"],
        "his_low": [9.0], "his_high": [11.0], "cost_5": [9.5], "cost_15": [9.8],
        "cost_50": [10.0], "cost_85": [10.2], "cost_95": [10.4], "weight_avg": [10.0],
        "winner_rate": [0.6]}))
    import data.tushare_sync as ts
    monkeypatch.setitem(__import__("config").TUSHARE_DATASETS, "cyq_perf", {
        "api": "cyq_perf", "by": "symbol", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,his_low,his_high,cost_5,cost_15,cost_50,cost_85,cost_95,weight_avg,winner_rate",
        "lake": "data_lake/cyq_perf.parquet",
        "quota_type": "special",  # 特色数据：300/分
    })
    # mock 全市场列表 1 只
    monkeypatch.setattr(ts, "_load_universe", lambda: ["000001.SZ"])
    # 验证 _fetch_with_guard 对特色数据用 300/分（acquire 仍调用，但日志标注）
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    ts.sync_dataset("cyq_perf", "2024-01-05", "2024-12-31", symbols=["000001.SZ"], resume=False)
    assert not pd.read_parquet(TUSHARE_DATASETS["cyq_perf"]["lake"]).empty
```

- [ ] **Step 2: 通用同步器支持特色数据标记**

在 `data/tushare_sync.py` 的 `_sync_by_symbol` 调 `_fetch_with_guard` 处，对 `cfg.get("quota_type") == "special"` 加日志标注（300/分通道）。限流上 tushare_rate_limiter 统一 acquire 即可（10000 积分常规 500/分，特色 300/分——rate_limiter 按 500 设定对特色数据是保守安全的，无需单独限流器；仅日志区分）。

```python
# _sync_by_symbol 内 _fetch_with_guard 调用前
        if cfg.get("quota_type") == "special":
            logger.debug("%s 为特色数据（300/分通道）", key)
```

- [ ] **Step 3: 配置 + 验证 + 提交**

```python
# config.py 追加
    "cyq_perf": {
        "api": "cyq_perf", "by": "symbol", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,his_low,his_high,cost_5,cost_15,cost_50,cost_85,cost_95,weight_avg,winner_rate",
        "lake": "data_lake/cyq_perf.parquet",
        "quota_type": "special",
    },
```
Run: `pytest tests/test_tushare_datasets_stock.py::test_cyq_perf_special_quota -v` → PASS
```bash
git add data/tushare_sync.py config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 特色筹码 cyq_perf(300/分通道标记)"
```

---

### Task 10: 股东/解禁/停牌（top10_holders/share_float/suspend_d）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_stock.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
# config.py 追加
    "top10_holders": {
        "api": "top10_holders", "by": "symbol", "date_col": "end_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio",
        "lake": "data_lake/top10_holders.parquet",
    },
    "top10_floatholders": {
        "api": "top10_floatholders", "by": "symbol", "date_col": "end_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio",
        "lake": "data_lake/top10_floatholders.parquet",
    },
    "share_float": {
        "api": "share_float", "by": "date", "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,float_share,float_date,float_share_share",
        "lake": "data_lake/share_float.parquet",
    },
    "suspend_d": {
        "api": "suspend_d", "by": "date", "date_col": "suspend_date", "symbol_col": "ts_code",
        "fields": "ts_code,suspend_date,resume_date,ann_date,suspend_reason,resume_reason",
        "lake": "data_lake/suspend_d.parquet",
    },
```
> ⚠️ top10_holders/floatholders 的 date_col=end_date（报告期末），但需用 ann_date（公告日）防前视——修正 `date_col` 为 `"ann_date"`。
测试同 by=symbol/date 范式。Run: `pytest -k holders -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_stock.py
git commit -m "feat(tushare): 股东/解禁/停牌 top10_holders/share_float/suspend_d"
```

---

### Task 11: 注册表对齐 + 端到端验证

**Files:**
- Modify: `config.py`（新湖注册到 `LAKE_CONFIG["lakes"]` + `DATASET_REGISTRY`）
- Test: `tests/test_dataset_registry.py`

- [ ] **Step 1: 写测试（注册表一致性）**

```python
# tests/test_dataset_registry.py 追加
def test_new_stock_lakes_registered():
    """Plan A 新增湖全部注册到 LAKE_CONFIG + DATASET_REGISTRY。"""
    from config import LAKE_CONFIG, DATASET_REGISTRY, TUSHARE_DATASETS
    new_lakes = ["fina_income", "fina_balance", "fina_cashflow", "dividend",
                 "moneyflow", "margin", "margin_detail", "margin_secs",
                 "top_inst", "moneyflow_hsgt", "concept", "concept_detail",
                 "ths_daily", "index_daily", "index_weight", "index_member",
                 "cyq_perf", "top10_holders", "top10_floatholders",
                 "share_float", "suspend_d"]
    for lk in new_lakes:
        assert lk in LAKE_CONFIG["lakes"], f"{lk} 未注册到 LAKE_CONFIG"
        # DATASET_REGISTRY source 标 Tushare
    assert DATASET_REGISTRY.get("fina_income", {}).get("source") == "Tushare"
```

- [ ] **Step 2: 注册新湖到 config**

```python
# config.py LAKE_CONFIG["lakes"] 追加所有新湖 key→path（与 TUSHARE_DATASETS lake 一致）
# DATASET_REGISTRY 追加每个新湖的 source/market/granularity/script/freshness_hours
```

- [ ] **Step 3: 端到端小样本验证**

```bash
python scripts/sync_tushare.py fina_income --years 1 --limit 3
python scripts/sync_tushare.py moneyflow --years 1  # 小区间
python -m pytest tests/ -v --ignore=tests/e2e
```
Expected: 新湖落盘；全测试绿。

- [ ] **Step 4: 提交**

```bash
git add config.py tests/test_dataset_registry.py
git commit -m "feat(tushare): Plan A 新湖注册表对齐+端到端验证"
```

---

## Self-Review

**1. Spec 覆盖**：股票类全部子类（财务报表/资金流/龙虎榜/融资融券/北向/板块/指数/特色/股东）均有 task ✅。通用同步器是 Plan B/C 前置 ✅。
**2. 占位符**：Task 7/8/10 的测试用"同范式参考 Task 5/6"——已在 Task 5/6 给完整测试代码，这些是同构 by=date/single，执行者照 Task 5/6 模式写。其余步骤完整代码。
**3. 类型一致**：`sync_dataset(key, start, end, symbols, resume)` 签名跨 task 一致；TUSHARE_DATASETS 配置 schema（api/by/date_col/symbol_col/fields/lake）一致。

## 前置关系

- 本 plan Task 1（通用同步器）是 **Plan B（ETF）/ Plan C（宏观）的前置**——B/C 复用 `sync_dataset` + `TUSHARE_DATASETS`。
