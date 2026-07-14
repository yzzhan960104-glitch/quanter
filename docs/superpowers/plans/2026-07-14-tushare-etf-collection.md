# Tushare ETF 专题采集 Implementation Plan（Plan B · 三类之二）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans。Steps use checkbox (`- [ ]`).
>
> **前置：Plan A Task 1（通用同步器 `data/tushare_sync.py` + `TUSHARE_DATASETS` 注册表）已实现。** 本 plan 复用 `sync_dataset` + 配置驱动，不再重复框架代码。

**Goal:** 采集 ETF 专题数据（列表/日线/净值/持仓/份额 + 跟踪指数成分），落 data_lake 湖。

**Architecture:** 各 ETF 接口在 `TUSHARE_DATASETS` 加配置（by=symbol/single），复用 Plan A 通用同步器。fund_basic 用 `market="EFT"` 筛 ETF；fund_daily/fund_nav/fund_portfolio/fund_share 按标的分页。

**Tech Stack:** 同 Plan A。10000 积分常规 500 次/分。

## Global Constraints

- 同 Plan A（Python 3.10 / 中文注释 / 限流复用 / 断点续传 / TDD + commits）。
- ETF 筛选：`fund_basic(market="EFT")` 返回 ETF 列表，作为 by=symbol 的标的池。
- fund_daily 与股票 daily 湖 schema 对齐（open/high/low/close/volume/amount）。

## File Structure

| 文件 | 责任 | 操作 |
|---|---|---|
| `config.py` | TUSHARE_DATASETS 追加 ETF 配置 + LAKE_CONFIG 注册 | 修改 |
| `data/tushare_sync.py` | 新增 `_load_etf_universe()` helper（fund_basic market=EFT） | 修改 |
| `tests/test_tushare_datasets_etf.py` | ETF 数据集配置+落湖测试 | 新建 |

---

### Task 1: fund_basic（ETF 列表，single 模式 + market=EFT 筛选）

**Files:**
- Modify: `config.py`、`data/tushare_sync.py`（新增 `_load_etf_universe`）
- Test: `tests/test_tushare_datasets_etf.py`

**Interfaces:**
- Produces: `TUSHARE_DATASETS["fund_basic"]`；`tushare_sync._load_etf_universe() -> list[str]`（ETF ts_code 列表）

- [ ] **Step 1: 写测试**

```python
# tests/test_tushare_datasets_etf.py
"""ETF 专题数据集配置 + 落湖测试。"""
import pandas as pd
import pytest
from config import TUSHARE_DATASETS


class _FakePro:
    def __init__(self): self._d = {}
    def set(self, api, df): self._d[api] = df
    def __getattr__(self, api):
        def _c(**kw): return self._d.get(api, pd.DataFrame())
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


def test_load_etf_universe_filters_market_eft(fake_pro):
    """fund_basic(market=EFT) 仅返回 ETF（筛 market=EFT）。"""
    fake_pro.set("fund_basic", pd.DataFrame({
        "ts_code": ["510300.SH", "510050.SH", "000001.OF"],
        "name": ["沪深300ETF", "50ETF", "华夏成长"],
        "market": ["EFT", "EFT", "OF"],  # 第三只是场外基金，排除
        "management": ["华泰柏瑞", "华夏", "华夏"],
    }))
    from data.tushare_sync import _load_etf_universe
    codes = _load_etf_universe()
    assert "510300.SH" in codes and "510050.SH" in codes
    assert "000001.OF" not in codes  # 场外基金排除


def test_fund_basic_single_lake(tmp_path, fake_pro, monkeypatch):
    fake_pro.set("fund_basic", pd.DataFrame({
        "ts_code": ["510300.SH"], "name": ["沪深300ETF"], "market": ["EFT"]}))
    monkeypatch.setitem(TUSHARE_DATASETS, "fund_basic", {
        "api": "fund_basic", "by": "single", "date_col": "found_date", "symbol_col": "ts_code",
        "fields": "ts_code,name,market,management,custodian,found_date,list_date",
        "lake": str(tmp_path / "fund_basic.parquet"),
    })
    import data.tushare_sync as ts
    ts.sync_dataset("fund_basic", "2024-01-01", "2024-12-31", resume=False)
    assert len(pd.read_parquet(TUSHARE_DATASETS["fund_basic"]["lake"])) == 1
```

- [ ] **Step 2: 实现 `_load_etf_universe` + 配置**

```python
# data/tushare_sync.py 追加
def _load_etf_universe() -> list[str]:
    """ETF 标的列表（fund_basic market='EFT'，场内 ETF）。"""
    df = _fetch_with_guard("fund_basic", market="EFT",
                           fields="ts_code,name,market,management,found_date,list_date")
    if df.empty:
        return []
    return df["ts_code"].tolist()
```

```python
# config.py TUSHARE_DATASETS 追加
    "fund_basic": {
        "api": "fund_basic", "by": "single", "date_col": "found_date", "symbol_col": "ts_code",
        "fields": "ts_code,name,market,management,custodian,found_date,list_date,issue_date,delist_date",
        "lake": "data_lake/fund_basic.parquet",
    },
```

- [ ] **Step 3-4: 验证 + 提交**

Run: `python -m pytest tests/test_tushare_datasets_etf.py -k fund_basic -v` → PASS
```bash
git add data/tushare_sync.py config.py tests/test_tushare_datasets_etf.py
git commit -m "feat(tushare): ETF 列表 fund_basic(market=EFT筛选)"
```

---

### Task 2: fund_daily（ETF 日线，by=symbol）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_etf.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
def test_fund_daily_by_symbol(tmp_path, fake_pro, monkeypatch):
    fake_pro.set("fund_daily", pd.DataFrame({
        "ts_code": ["510300.SH"] * 2, "trade_date": ["20240105", "20240108"],
        "open": [4.1, 4.2], "high": [4.2, 4.3], "low": [4.0, 4.1],
        "close": [4.15, 4.25], "vol": [1e7, 1.1e7], "amount": [4.2e7, 4.6e7]}))
    monkeypatch.setitem(TUSHARE_DATASETS, "fund_daily", {
        "api": "fund_daily", "by": "symbol", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "lake": str(tmp_path / "etf_daily.parquet"),
    })
    import data.tushare_sync as ts
    ts.sync_dataset("fund_daily", "2024-01-05", "2024-01-10",
                    symbols=["510300.SH"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fund_daily"]["lake"])
    assert df.index.names == ["date", "symbol"] and len(df) == 2
    assert "close" in df.columns
```

```python
# config.py 追加（fund_daily: vol→volume 归一在 _cleanse 后，或保留 vol）
    "fund_daily": {
        "api": "fund_daily", "by": "symbol", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "lake": "data_lake/etf_daily.parquet",
    },
```
> ⚠️ fund_daily 返回 `vol`（非 volume）。_cleanse 不改列名，落湖保留 vol，或在 sync_dataset 加列名归一。建议：TUSHARE_DATASETS 配置加 `rename: {"vol": "volume"}`，通用同步器落湖前应用 rename（实现时在 `_cleanse` 后加 `df = df.rename(columns=cfg.get("rename", {}))`）。
Run: `pytest -k fund_daily -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_etf.py
git commit -m "feat(tushare): ETF 日线 fund_daily(by=symbol)"
```

---

### Task 3: fund_nav（ETF 净值，by=symbol）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_etf.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
def test_fund_nav_by_symbol(tmp_path, fake_pro, monkeypatch):
    fake_pro.set("fund_nav", pd.DataFrame({
        "ts_code": ["510300.SH"] * 2, "nav_date": ["20240105", "20240108"],
        "unit_nav": [4.15, 4.25], "accum_nav": [4.15, 4.25]}))
    monkeypatch.setitem(TUSHARE_DATASETS, "fund_nav", {
        "api": "fund_nav", "by": "symbol", "date_col": "nav_date", "symbol_col": "ts_code",
        "fields": "ts_code,nav_date,unit_nav,accum_nav,accum_nav_rate",
        "lake": str(tmp_path / "etf_nav.parquet"),
    })
    import data.tushare_sync as ts
    ts.sync_dataset("fund_nav", "2024-01-05", "2024-01-10",
                    symbols=["510300.SH"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fund_nav"]["lake"])
    assert "unit_nav" in df.columns and len(df) == 2
```

```python
# config.py 追加
    "fund_nav": {
        "api": "fund_nav", "by": "symbol", "date_col": "nav_date", "symbol_col": "ts_code",
        "fields": "ts_code,nav_date,unit_nav,accum_nav,accum_nav_rate",
        "lake": "data_lake/etf_nav.parquet",
    },
```
Run: `pytest -k fund_nav -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_etf.py
git commit -m "feat(tushare): ETF 净值 fund_nav"
```

---

### Task 4: fund_portfolio（ETF 持仓，by=symbol）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_etf.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
def test_fund_portfolio_by_symbol(tmp_path, fake_pro, monkeypatch):
    fake_pro.set("fund_portfolio", pd.DataFrame({
        "ts_code": ["510300.SH"], "end_date": ["20231231"],
        "symbol": ["600519.SH"], "name": ["贵州茅台"], "amount": [1e6], "stk_value": [1.8e9]}))
    monkeypatch.setitem(TUSHARE_DATASETS, "fund_portfolio", {
        "api": "fund_portfolio", "by": "symbol", "date_col": "end_date", "symbol_col": "ts_code",
        "fields": "ts_code,end_date,symbol,name,amount,stk_value,stk_value_ratio",
        "lake": str(tmp_path / "etf_portfolio.parquet"),
    })
    import data.tushare_sync as ts
    ts.sync_dataset("fund_portfolio", "2024-01-05", "2024-12-31",
                    symbols=["510300.SH"], resume=False)
    assert not pd.read_parquet(TUSHARE_DATASETS["fund_portfolio"]["lake"]).empty
```

```python
# config.py 追加
    "fund_portfolio": {
        "api": "fund_portfolio", "by": "symbol", "date_col": "end_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,symbol,name,amount,stk_value,stk_value_ratio",
        "lake": "data_lake/etf_portfolio.parquet",
    },
```
> ⚠️ fund_portfolio 的 date_col：用 ann_date（公告日）防前视（持仓数据公告滞后），修正 `date_col` 为 `"ann_date"`。
Run: `pytest -k portfolio -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_etf.py
git commit -m "feat(tushare): ETF 持仓 fund_portfolio(ann_date防前视)"
```

---

### Task 5: fund_share（ETF 份额，by=symbol）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_etf.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
# config.py 追加
    "fund_share": {
        "api": "fund_share", "by": "symbol", "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,share_unissue,total_share,float_share",
        "lake": "data_lake/etf_share.parquet",
    },
```
测试同 by=symbol 范式（参考 Task 3 fund_nav）。Run: `pytest -k fund_share -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_etf.py
git commit -m "feat(tushare): ETF 份额 fund_share"
```

---

### Task 6: ETF 跟踪指数成分复用 + 注册表对齐

**Files:**
- Modify: `config.py`（LAKE_CONFIG/DATASET_REGISTRY 注册 ETF 湖）
- Test: `tests/test_dataset_registry.py`

- [ ] **Step 1: 测试（ETF 湖注册 + index_member 共用）**

```python
# tests/test_dataset_registry.py 追加
def test_etf_lakes_registered():
    from config import LAKE_CONFIG, DATASET_REGISTRY
    etf_lakes = ["fund_basic", "fund_daily", "fund_nav", "fund_portfolio", "fund_share"]
    for lk in etf_lakes:
        assert lk in LAKE_CONFIG["lakes"], f"{lk} 未注册"
    # index_member/index_weight 由 Plan A 提供，ETF 跟踪指数成分复用
    assert "index_member" in LAKE_CONFIG["lakes"]
```

- [ ] **Step 2: 注册 ETF 湖到 config**

```python
# config.py LAKE_CONFIG["lakes"] 追加
    "fund_basic": "data_lake/fund_basic.parquet",
    "fund_daily": "data_lake/etf_daily.parquet",
    "fund_nav": "data_lake/etf_nav.parquet",
    "fund_portfolio": "data_lake/etf_portfolio.parquet",
    "fund_share": "data_lake/etf_share.parquet",
# DATASET_REGISTRY 追加各 ETF 湖 source=Tushare
```

- [ ] **Step 3: 端到端小样本验证**

```bash
python scripts/sync_tushare.py fund_basic
python scripts/sync_tushare.py fund_daily --years 1 --limit 3  # 注：--limit 走 _load_universe（股票），
                                                              # ETF 需改 sync_tushare.py 支持 --etf 用 _load_etf_universe
```
> ⚠️ sync_tushare.py 的 `--limit` 当前用 `_load_universe`（股票）。ETF 需加 `--etf` 标志走 `_load_etf_universe`。实现 Task：在 sync_tushare.py 加 `--market {stock,etf}` 参数，etf 时 symbols=`_load_etf_universe()[:limit]`。

- [ ] **Step 4: 提交**

```bash
git add config.py tests/test_dataset_registry.py scripts/sync_tushare.py
git commit -m "feat(tushare): Plan B ETF 湖注册+sync_tushare支持--market etf"
```

---

## Self-Review

**1. Spec 覆盖**：ETF 列表/日线/净值/持仓/份额 + 指数成分（Plan A 共用）全覆盖 ✅。
**2. 占位符**：Task 5 测试"参考 Task 3 范式"——Task 3 已给完整 fund_nav 测试，fund_share 同构。其余完整。
**3. 类型一致**：配置 schema 与 Plan A 一致；`_load_etf_universe` 新增 helper。
