# Tushare 宏观经济采集 Implementation Plan（Plan C · 三类之三）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans。Steps use checkbox (`- [ ]`).
>
> **前置：Plan A Task 1（通用同步器）已实现。** 本 plan 复用 `sync_dataset`，并重写 `sync_macro_credit` 切 Tushare。

**Goal:** 宏观经济数据切 Tushare（cn_m/cn_cpi/cn_ppi/cn_gdp/cn_pmi + shibor + 交易所成交统计），社融/DR007 走 akshare fallback；保证 macro 湖列名对齐 CreditRegime（`shrzgm`/`M1M2_gap`/`dr007`），CreditRegime 代码不改。

**Architecture:** 通用同步器加 `index_mode=datetime` 支持（宏观湖 DatetimeIndex 无 symbol）；`sync_macro_credit` 重写为 Tushare cn_m（M0/M1/M2）+ akshare 社融/DR007 fallback → align_to_daily ffill-only → 衍生 M1M2_gap → macro 湖。前视红线不变。

**Tech Stack:** 同 Plan A。宏观月频/季频，频次充裕（500/分）。

## Global Constraints

- 同 Plan A。宏观前视红线：月频仅向前 ffill，绝无 bfill（`align_to_daily` 既有契约）。
- CreditRegime 不变量（`core/macro_regime.py:154`）：macro 湖必须含 `shrzgm` + `M1M2_gap` 列，dr007 可选。
- 社融 shrzgm / DR007：Tushare 无专门接口 → akshare fallback（spec §3.7 风险条款）。

## File Structure

| 文件 | 责任 | 操作 |
|---|---|---|
| `data/tushare_sync.py` | `_sync_single` 加 `index_mode=datetime` 支持 | 修改 |
| `scripts/sync_macro_credit.py` | 重写：Tushare cn_m + akshare fallback → macro 湖 | 重写 |
| `config.py` | TUSHARE_DATASETS 追加宏观配置 + macro source 改 Tushare | 修改 |
| `tests/test_tushare_datasets_macro.py` | 宏观数据集 + CreditRegime 列名对齐测试 | 新建 |

---

### Task 1: 通用同步器加 index_mode=datetime（宏观湖 DatetimeIndex）

**Files:**
- Modify: `data/tushare_sync.py`（`_sync_single` 支持 DatetimeIndex）
- Test: `tests/test_tushare_datasets_macro.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_tushare_datasets_macro.py
"""宏观经济数据集 + CreditRegime 列名对齐测试。"""
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


def test_cpi_lake_datetime_index(tmp_path, fake_pro, monkeypatch):
    """cn_cpi 宏观湖：DatetimeIndex（无 symbol 层），非 MultiIndex。"""
    fake_pro.set("cn_cpi", pd.DataFrame({
        "month": ["202401", "202402"], "nt_yoy": [0.5, -0.3]}))  # 全国同比
    monkeypatch.setitem(TUSHARE_DATASETS, "cn_cpi", {
        "api": "cn_cpi", "by": "single", "date_col": "month", "symbol_col": "month",
        "fields": "month,nt_yoy",
        "index_mode": "datetime",  # DatetimeIndex 而非 MultiIndex
        "lake": str(tmp_path / "cpi.parquet"),
    })
    import data.tushare_sync as ts
    ts.sync_dataset("cn_cpi", "2024-01-01", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["cn_cpi"]["lake"])
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == "month" or df.index.name == "date"
    assert "nt_yoy" in df.columns
```

- [ ] **Step 2: 改 `_sync_single` 支持 index_mode=datetime**

```python
# data/tushare_sync.py —— _sync_single 修改
def _sync_single(key, api, fields, date_col, out, cfg=None):
    """单次拉取（指数/列表/宏观）。index_mode=datetime 时落 DatetimeIndex。"""
    kwargs = {}
    if fields:
        kwargs["fields"] = fields
    df = _fetch_with_guard(api, **kwargs)
    if df.empty:
        logger.warning("%s 数据为空，跳过", key)
        return
    cfg = cfg or TUSHARE_DATASETS[key]
    if cfg.get("index_mode") == "datetime" and date_col in df.columns:
        # 宏观湖：DatetimeIndex（无 symbol 层）
        df[date_col] = pd.to_datetime(df[date_col], format="%Y%m", errors="coerce")
        df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    df.to_parquet(out, engine="pyarrow")
    logger.info("%s 写入：%s，%d 行", key, out, len(df))
```

同步修改 `sync_dataset` 里 `_sync_single(key, api, fields, date_col, out)` 调用为传 cfg：`_sync_single(key, api, fields, date_col, out, cfg=cfg)`。

- [ ] **Step 3-4: 验证 + 提交**

Run: `python -m pytest tests/test_tushare_datasets_macro.py::test_cpi_lake_datetime_index -v` → PASS
```bash
git add data/tushare_sync.py tests/test_tushare_datasets_macro.py
git commit -m "feat(tushare): 通用同步器支持 index_mode=datetime(宏观湖DatetimeIndex)"
```

---

### Task 2: 重写 sync_macro_credit（Tushare cn_m + akshare 社融/DR007 fallback + CreditRegime 列名对齐）

**Files:**
- Rewrite: `scripts/sync_macro_credit.py`
- Test: `tests/test_tushare_datasets_macro.py`

**Interfaces:**
- Produces: `data_lake/macro_credit.parquet`（DatetimeIndex，列含 `shrzgm` + `M1M2_gap`，dr007 可选）
- CreditRegime (`core/macro_regime.py`) **不改**：本 Task 验证列名对齐。

- [ ] **Step 1: 写测试（CreditRegime 列名契约 + M1M2_gap 衍生）**

```python
def test_macro_lake_credit_regime_columns(tmp_path, fake_pro, monkeypatch):
    """macro 湖必须含 shrzgm + M1M2_gap（CreditRegime core 字段契约）。"""
    # Tushare cn_m：M0/M1/M2 同比
    fake_pro.set("cn_m", pd.DataFrame({
        "month": ["202401", "202402"],
        "m0_yoy": [8.0, 8.5], "m1_yoy": [5.9, 6.6], "m2_yoy": [8.7, 8.7]}))
    # akshare 社融/DR007 fallback（mock）
    import data.clients.akshare_client as akc
    monkeypatch.setattr(akc.AKShareClient, "fetch_macro_raw",
                        lambda self, kind: {
                            "shrzgm": pd.DataFrame({"月份": ["202401", "202402"], "社融增量": [50000, 60000]}),
                            "dr007": pd.DataFrame({"日期": ["2024-01-05", "2024-02-05"], "DR007": [1.9, 1.8]}),
                        }.get(kind, pd.DataFrame()))
    out = str(tmp_path / "macro.parquet")
    from scripts.sync_macro_credit import sync_macro
    sync_macro("2024-01-01", "2024-02-28", out=out)
    df = pd.read_parquet(out)
    assert "shrzgm" in df.columns, "CreditRegime core 字段 shrzgm 缺失"
    assert "M1M2_gap" in df.columns, "CreditRegime core 字段 M1M2_gap 缺失"
    # M1M2_gap = M1同比 - M2同比
    assert df["M1M2_gap"].notna().any()


def test_credit_regime_unchanged_reads_columns(fake_pro, monkeypatch):
    """CreditRegime 代码不改，验证其消费 macro 湖列名。"""
    from core.macro_regime import CreditRegime
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    macro = pd.DataFrame({"shrzgm": range(30), "M1M2_gap": [1.0] * 30,
                          "dr007": [2.0] * 30}, index=idx)
    r = CreditRegime(macro_df=macro)
    assert r.compute(idx[-1]) in (1, 0, -1)  # 列名对齐，不抛
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_tushare_datasets_macro.py::test_macro_lake_credit_regime_columns -v`
Expected: FAIL（sync_macro 仍走 akshare）

- [ ] **Step 3: 重写 sync_macro_credit.py**

```python
"""宏观信贷同步：Tushare cn_m(M0/M1/M2) + akshare fallback(社融/DR007) → 日频对齐 parquet。

源切换（用户决策：宏观切 Tushare）：
- M0/M1/M2 → Tushare cn_m（聚宽/Tushare 宏观覆盖）
- 社融(shrzgm)/DR007 → Tushare 无专门接口，fallback akshare 单指标

CreditRegime 不变量（core/macro_regime.py:154）：macro 湖必须含 shrzgm + M1M2_gap
列，dr007 可选。本脚本保证列名对齐——CreditRegime 代码不改。

前视红线（不变）：align_to_daily 仅向前 ffill，绝无 bfill。
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from config import LAKE_CONFIG
from data._tushare_compat import get_pro
from data.resilience import tushare_rate_limiter, tushare_breaker
from data.clients.akshare_client import AKShareClient

# align_to_daily / _pick 保留原样（前视红线 ffill-only 实现，源切换不改对齐逻辑）
# 从原 sync_macro_credit.py 复制这两个函数到本文件（不修改）


def _fetch_with_guard(api_name: str, **kwargs) -> pd.DataFrame:
    """限频+熔断包装（与 tushare_sync 同范式）。"""
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        return pd.DataFrame()
    pro = get_pro()
    try:
        df = getattr(pro, api_name)(**kwargs)
    except Exception as e:
        tushare_breaker.record_failure()
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    tushare_breaker.record_success()
    return df


def fetch_macro_series(start: str, end: str) -> pd.DataFrame:
    """合并 M0/M1/M2(Tushare cn_m) + 社融/DR007(akshare fallback) → 日频对齐。

    M1M2_gap 衍生 = M1同比 - M2同比（CreditRegime core 字段）。
    """
    series: dict[str, pd.Series] = {}

    # M0/M1/M2 走 Tushare cn_m（月频）
    cn_m = _fetch_with_guard("cn_m", start_m=start.replace("-", "")[:6],
                             end_m=end.replace("-", "")[:6])
    if not cn_m.empty:
        m = align_to_daily(_pick(cn_m, "month"), "month", start, end)
        for col, key in [("m1_yoy", "M1同比增长"), ("m2_yoy", "M2同比增长")]:
            if col in m.columns:
                series[key] = pd.to_numeric(m[col], errors="coerce")

    # 社融 shrzgm + DR007：akshare fallback（Tushare 无专门接口）
    ak = AKShareClient()
    shrzgm = ak.fetch_macro_raw("shrzgm")
    if not shrzgm.empty:
        s = align_to_daily(_pick(shrzgm, "月份"), "月份", start, end)
        series["shrzgm"] = s.iloc[:, 0]
    dr007 = ak.fetch_macro_raw("dr007")
    if not dr007.empty:
        d = align_to_daily(_pick(dr007, "日期"), "日期", start, end)
        series["dr007"] = d.iloc[:, 0]

    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(series).ffill().dropna(how="all")
    if "M1同比增长" in df and "M2同比增长" in df:
        df["M1M2_gap"] = pd.to_numeric(df["M1同比增长"], errors="coerce") \
                         - pd.to_numeric(df["M2同比增长"], errors="coerce")
    return df


def sync_macro(start: str, end: str, out: str | None = None) -> None:
    """落 data_lake/macro_credit.parquet（DatetimeIndex，列含 shrzgm + M1M2_gap）。"""
    out = out or LAKE_CONFIG["lakes"]["macro"]
    df = fetch_macro_series(start, end)
    if df.empty:
        print("宏观数据为空，跳过")
        return
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out)
    print(f"宏观湖写入：{out}，{len(df)} 行，列={list(df.columns)}")


if __name__ == "__main__":
    import datetime as _dt
    end = _dt.date.today().strftime("%Y-%m-%d")
    start = (_dt.date.today() - _dt.timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    sync_macro(start, end)
```

> ⚠️ **事实审查**：Tushare `cn_m` 接口的参数是 `start_m`/`end_m`（6 位 YYYYMM）还是 `start_date`/`end_date`？字段是 `m0_yoy`/`m1_yoy`/`m2_yoy`？需用 `python -c "import tushare as ts; pro=ts.pro_api('token'); print(pro.cn_m(start_m='202401', end_m='202402').columns)"` 探测确认。若参数/字段名不同，调整 `_fetch_with_guard("cn_m", ...)` 与列对齐逻辑。

- [ ] **Step 4: 验证 + 提交**

Run: `python -m pytest tests/test_tushare_datasets_macro.py -k "credit_regime or macro_lake" -v` → PASS
```bash
git add scripts/sync_macro_credit.py tests/test_tushare_datasets_macro.py
git commit -m "refactor(sync): sync_macro_credit 切 Tushare cn_m(M1/M2)+akshare社融/DR007 fallback,CreditRegime不变"
```

---

### Task 3: cn_cpi/cn_ppi/cn_gdp/cn_pmi（宏观指标原始湖）

**Files:**
- Modify: `config.py`（追加 4 个宏观配置，index_mode=datetime）
- Test: `tests/test_tushare_datasets_macro.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
# config.py TUSHARE_DATASETS 追加
    "cn_cpi": {
        "api": "cn_cpi", "by": "single", "date_col": "month", "symbol_col": "month",
        "fields": "month,nt_yoy,nt_mom,yty_yoy",
        "index_mode": "datetime", "lake": "data_lake/cn_cpi.parquet",
    },
    "cn_ppi": {
        "api": "cn_ppi", "by": "single", "date_col": "month", "symbol_col": "month",
        "fields": "month,ppi_yoy,ppi_mom",
        "index_mode": "datetime", "lake": "data_lake/cn_ppi.parquet",
    },
    "cn_gdp": {
        "api": "cn_gdp", "by": "single", "date_col": "quarter", "symbol_col": "quarter",
        "fields": "quarter,gdp,gdp_yoy,pi,si,ti",
        "index_mode": "datetime", "lake": "data_lake/cn_gdp.parquet",
    },
    "cn_pmi": {
        "api": "cn_pmi", "by": "single", "date_col": "month", "symbol_col": "month",
        "fields": "month,ppi_yoy,ppi_mom,business_index_pmi,manufacturing_pmi",
        "index_mode": "datetime", "lake": "data_lake/cn_pmi.parquet",
    },
```
测试同 Task 1 cn_cpi 范式（DatetimeIndex）。Run: `pytest -k "cpi or ppi or gdp or pmi" -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_macro.py
git commit -m "feat(tushare): 宏观指标 cn_cpi/ppi/gdp/pmi(index_mode=datetime)"
```

---

### Task 4: shibor/shibor_quote（银行间拆放）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_macro.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
# config.py 追加
    "shibor": {
        "api": "shibor", "by": "single", "date_col": "date", "symbol_col": "date",
        "fields": "date,1w,2w,1m,3m,6m,9m,1y",
        "index_mode": "datetime", "lake": "data_lake/shibor.parquet",
    },
    "shibor_quote": {
        "api": "shibor_quote", "by": "single", "date_col": "date", "symbol_col": "date",
        "fields": "date,bank,1w,2w,1m,3m,6m,9m,1y",
        "index_mode": "datetime", "lake": "data_lake/shibor_quote.parquet",
    },
```
测试同 Task 1（DatetimeIndex）。Run: `pytest -k shibor -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_macro.py
git commit -m "feat(tushare): shibor/shibor_quote 银行间拆放"
```

---

### Task 5: szse_daily/sse_daily（交易所成交统计 → mkt_daily 湖）

**Files:**
- Modify: `config.py`
- Test: `tests/test_tushare_datasets_macro.py`

- [ ] **Step 1-4: 测试 + 配置 + 验证 + 提交**

```python
# config.py 追加（by=date，市场级时序）
    "szse_daily": {
        "api": "szse_daily", "by": "date", "date_col": "trade_date", "symbol_col": "trade_date",
        "fields": "trade_date,issuer_num,sec_num,total_share,total_value,pe",
        "lake": "data_lake/mkt_daily_szse.parquet",
    },
    "sse_daily": {
        "api": "sse_daily", "by": "date", "date_col": "trade_date", "symbol_col": "trade_date",
        "fields": "trade_date,issuer_num,sec_num,total_share,total_value,pe",
        "lake": "data_lake/mkt_daily_sse.parquet",
    },
```
测试同 by=date 范式（Plan A Task 3）。Run: `pytest -k "szse or sse" -v` → PASS
```bash
git add config.py tests/test_tushare_datasets_macro.py
git commit -m "feat(tushare): 交易所成交统计 szse_daily/sse_daily"
```

---

### Task 6: 注册表对齐 + CreditRegime 不变性 + 端到端

**Files:**
- Modify: `config.py`（宏观湖注册 LAKE_CONFIG/DATASET_REGISTRY；macro source 改 Tushare）
- Test: `tests/test_dataset_registry.py`

- [ ] **Step 1: 测试（macro source 改 Tushare + 新湖注册 + CreditRegime 端到端）**

```python
# tests/test_dataset_registry.py 追加
def test_macro_source_changed_to_tushare():
    from config import DATASET_REGISTRY
    # macro 切 Tushare（cn_m 主源 + akshare 社融 fallback）
    assert DATASET_REGISTRY["macro"]["source"] == "Tushare"

def test_macro_lakes_registered():
    from config import LAKE_CONFIG
    for lk in ["cn_cpi", "cn_ppi", "cn_gdp", "cn_pmi", "shibor", "shibor_quote",
               "mkt_daily_szse", "mkt_daily_sse"]:
        assert lk in LAKE_CONFIG["lakes"], f"{lk} 未注册"

def test_credit_regime_end_to_end_with_tushare_macro(tmp_path, fake_pro, monkeypatch):
    """端到端：sync_macro 落湖 → CreditRegime.compute 不抛。"""
    fake_pro.set("cn_m", pd.DataFrame({
        "month": ["202301"] * 25,  # 25 期保证 >_MIN_LOOKBACK(20)
        "m1_yoy": [5.0] * 25, "m2_yoy": [8.0] * 25, "m0_yoy": [8.0] * 25}))
    import data.clients.akshare_client as akc
    monkeypatch.setattr(akc.AKShareClient, "fetch_macro_raw",
                        lambda self, kind: pd.DataFrame({"月份": ["202301"] * 25,
                                                         "社融增量": [50000] * 25})
                        if kind == "shrzgm" else pd.DataFrame())
    out = str(tmp_path / "macro.parquet")
    from scripts.sync_macro_credit import sync_macro
    sync_macro("2023-01-01", "2025-01-01", out=out)
    from core.macro_regime import CreditRegime
    df = pd.read_parquet(out)
    r = CreditRegime(macro_df=df).compute(df.index[-1])
    assert r in (1, 0, -1)
```

- [ ] **Step 2: config 注册（macro source 改 Tushare + 新湖）**

```python
# config.py DATASET_REGISTRY["macro"] source 改 "Tushare"
    "macro": {"source": "Tushare", "market": "宏观", ...}  # 原 AKShare 改 Tushare
# LAKE_CONFIG["lakes"] 追加 cn_cpi/cn_ppi/.../mkt_daily_* 路径
```

- [ ] **Step 3: 端到端验证**

```bash
python scripts/sync_macro_credit.py
python scripts/sync_tushare.py cn_cpi
python -c "
import pandas as pd
from core.macro_regime import CreditRegime
m = pd.read_parquet('data_lake/macro_credit.parquet')
assert 'shrzgm' in m.columns and 'M1M2_gap' in m.columns
print('CreditRegime:', CreditRegime.get_default().compute(m.index[-1]))
"
python -m pytest tests/ -v --ignore=tests/e2e
```
Expected: macro 湖含 shrzgm+M1M2_gap；CreditRegime 返 1/0/-1；全测试绿。

- [ ] **Step 4: 提交**

```bash
git add config.py tests/test_dataset_registry.py
git commit -m "feat(tushare): Plan C 宏观湖注册+macro切Tushare+CreditRegime端到端验证"
```

---

## Self-Review

**1. Spec 覆盖**：宏观 cn_*/shibor/交易所统计 + macro 切源 + CreditRegime 列名对齐全覆盖 ✅。
**2. 占位符**：Task 2 标注 cn_m 接口参数/字段待探测（事实风险，附探测命令，spec 已声明风险）；Task 3-5 测试同 Task 1 范式（Task 1 已给完整 DatetimeIndex 测试）。其余完整。
**3. 类型一致**：`index_mode=datetime` 配置跨宏观 task 一致；macro 湖列名 `shrzgm`/`M1M2_gap`/`dr007` 与 `core/macro_regime.py:154` 一致。

## 三类 Plan 完成

- Plan A（股票高价值 + 通用同步器基础）：`2026-07-14-tushare-stock-collection.md`
- Plan B（ETF专题）：`2026-07-14-tushare-etf-collection.md`
- Plan C（宏观经济）：`2026-07-14-tushare-macro-collection.md`
- 执行序：A → B → C（B/C 依赖 A 的通用同步器）
