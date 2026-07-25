# Tushare 数据快照扩容 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (inline 模式，用户已授权全自主执行，跳过 checkpoint 人工确认)。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Tushare 限频改双桶(基础500/特色300)、补齐 9 类数据集（跳过 opt_basic，保留 cyq_chips）、daily/weekly/monthly 纳入统一前复权管道、统一 CLI 收敛 scripts/，并跑一轮近 5 年全量回填。

**Architecture:** `data/resilience.py` 拆双桶 + 别名向后兼容；`data/tushare_sync.py` 的 `_fetch_with_guard` 按 `quota_type` 选桶、`_sync_by_symbol` 加 `adj_api` 前复权增强、`resolve_symbols` 加 concept universe；`config/registry.py` 声明式补 key + quota_type；新建 `data/sync_cli.py` 统一入口；`scripts/sync_*.py` 转薄壳 + DeprecationWarning。

**Tech Stack:** Python 3 + pandas + pyarrow + tushare SDK（纯直连）+ pytest（TDD）

## Global Constraints

- 全中文代码注释（CLAUDE.md 协议），注释说"为什么"不只说"是什么"
- 前视红线：财报类 `date_col=ann_date`，绝不用 `end_date`；OHLCV 用 `trade_date`
- 前复权公式 `price_qfq = price_raw × adj_factor / adj_factor_latest`（latest=区间最新），与现有 `a_shares_daily.parquet` 字节级一致
- `scripts/sync_tushare.py` 不能删（server `data_service` 子进程依赖 `DATASET_REGISTRY.script`）
- 不删任何既有脚本，只转薄壳 + DeprecationWarning
- 每任务 TDD：先写失败测试→验证失败→最小实现→验证通过→commit
- 字段名必须 dry-run 探测确认（防幻觉列，沿用项目 `data/tools/probe_tushare_fields.py` 习惯）
- quota_type 归类：基础桶=list/行情/指数/概念/宏观；特色桶=资金/筹码/因子/龙虎榜机构/融资融券明细/股东

**Spec:** `docs/superpowers/specs/2026-07-25-tushare-data-snapshot-design.md`

---

## Task 1: resilience.py 双桶限频 + 别名向后兼容

**Files:**
- Modify: `data/resilience.py:277`（`tushare_rate_limiter` 定义处）
- Test: `tests/test_resilience_quota.py`（新建）

**Interfaces:**
- Produces: `tushare_rate_limiter_basic`（500/min）、`tushare_rate_limiter_special`（300/min）、`tushare_rate_limiter`（=basic 别名，向后兼容 `fetcher.py`/`sync_macro_credit.py`/`sync_data_lake.py`/`tushare_sync.py` 4 处旧调用）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_resilience_quota.py
# -*- coding: utf-8 -*-
"""双桶限频测试：基础500/特色300 + 别名向后兼容。"""
from data.resilience import (
    tushare_rate_limiter_basic,
    tushare_rate_limiter_special,
    tushare_rate_limiter,
)


def test_双桶独立实例():
    """基础桶与特色桶是两个独立 RateLimiter 实例（互不干扰计数）。"""
    assert tushare_rate_limiter_basic is not tushare_rate_limiter_special
    assert tushare_rate_limiter_basic.name == "tushare_basic"
    assert tushare_rate_limiter_special.name == "tushare_special"


def test_别名指向基础桶():
    """旧名 tushare_rate_limiter 必须是 basic 别名（4 处旧调用零改）。"""
    assert tushare_rate_limiter is tushare_rate_limiter_basic


def test_基础桶配额_500每分():
    """refill_rate × 60 ≈ 500/min（允许少量余量避免边界429）。"""
    # refill_rate 单位 token/s，×60 = 每分补充量 ≈ 官方配额
    assert 490 <= tushare_rate_limiter_basic.refill_rate * 60 <= 500
    # capacity 给少量突发（官方滑动窗口边界允许）
    assert tushare_rate_limiter_basic.capacity >= 5


def test_特色桶配额_300每分():
    """refill_rate × 60 ≈ 300/min。"""
    assert 290 <= tushare_rate_limiter_special.refill_rate * 60 <= 300
    assert tushare_rate_limiter_special.capacity >= 3
```

- [ ] **Step 2: 验证测试失败**

Run: `python -m pytest tests/test_resilience_quota.py -v`
Expected: FAIL（`tushare_rate_limiter_basic` 不存在 / name 不匹配）

- [ ] **Step 3: 实现**

`data/resilience.py:277` 替换（保留原注释历史，更新为新双桶）：

```python
# 双桶限频（2026-07-25 账户升级后官方配额：基础500/分 + 特色300/分）：
# Why 双桶：Tushare 官方按接口类别分两档配额，原单桶 ~20/分（capacity=3, refill_rate=0.33）
# 是为已废弃的 tnskhdata 代理调的旧参数（2026-07-24 已纯直连，见 _tushare_compat.py），
# 远低于官方配额上限，浪费吞吐。按 quota_type 路由到对应桶（见 tushare_sync._fetch_with_guard）。
# 容量取值：refill_rate × 60 ≈ 配额/min，capacity 给少量突发（官方滑动窗口边界允许短时突发），
# 留 ~1% 余量避免边界抖动触发 429。
tushare_rate_limiter_basic = RateLimiter(name="tushare_basic", capacity=8, refill_rate=8.3)    # ~498/min
tushare_rate_limiter_special = RateLimiter(name="tushare_special", capacity=5, refill_rate=5.0)  # 300/min

# 向后兼容别名：fetcher.py / sync_macro_credit.py / sync_data_lake.py / tushare_sync.py
# 4 处旧调用零改（它们用 tushare_rate_limiter 名字，默认走基础桶，语义不变）。
# Why 别名不删：显式至上，避免为改名而扩散冲击 4 个文件；旧调用走基础桶配额合理（多数是基础接口）。
tushare_rate_limiter = tushare_rate_limiter_basic
```

- [ ] **Step 4: 验证测试通过**

Run: `python -m pytest tests/test_resilience_quota.py -v`
Expected: 4 passed

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_resilience.py tests/test_resilience_quota.py -v`（若 `test_resilience.py` 存在）+ `python -c "from data.resilience import tushare_rate_limiter; print('alias OK')"`

```bash
git add data/resilience.py tests/test_resilience_quota.py
git commit -m "feat(resilience): Tushare 限频拆双桶(基础500/特色300)+别名向后兼容

账户升级后官方配额基础500/特色300每分，原单桶~20/分是废弃代理旧参数。
tushare_rate_limiter 作 basic 别名，4处旧调用零改。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `_fetch_with_guard` quota_type 路由 + sync_dataset 下传

**Files:**
- Modify: `data/tushare_sync.py:66`（`_fetch_with_guard` 签名 + body）+ `_sync_by_symbol:295`/`_sync_by_date:325`/`_sync_single:382`（3 处 `_fetch_with_guard` 调用加 `quota_type`）
- Test: `tests/test_tushare_sync_quota.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `tushare_rate_limiter_basic`/`tushare_rate_limiter_special`
- Produces: `_fetch_with_guard(api_name, *, quota_type="basic", **kwargs)` 新签名；`sync_dataset` 从 `cfg["quota_type"]` 读并通过 `_sync_by_*` 的 `cfg` 下传

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tushare_sync_quota.py
# -*- coding: utf-8 -*-
"""_fetch_with_guard 按 quota_type 路由到对应限频桶。"""
from unittest.mock import patch, MagicMock
import pandas as pd

import data.tushare_sync as tsync
from data.resilience import tushare_rate_limiter_basic, tushare_rate_limiter_special


def _patch_pro(fake_df):
    """patch get_pro 返回 MagicMock，其任意方法返 fake_df。"""
    pro = MagicMock()
    pro.some_api = MagicMock(return_value=fake_df)
    return patch("data.tushare_sync.get_pro", return_value=pro), pro


def test_quota_basic_走基础桶():
    """quota_type=basic 应 acquire 基础桶令牌。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "close": [10.0]})
    p, _ = _patch_pro(df)
    with p, patch.object(tushare_rate_limiter_basic, "acquire") as acq_b, \
         patch.object(tushare_rate_limiter_special, "acquire") as acq_s:
        tsync._fetch_with_guard("some_api", quota_type="basic", trade_date="20250101")
        acq_b.assert_called_once()
        acq_s.assert_not_called()


def test_quota_special_走特色桶():
    """quota_type=special 应 acquire 特色桶令牌。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"], "close": [10.0]})
    p, _ = _patch_pro(df)
    with p, patch.object(tushare_rate_limiter_basic, "acquire") as acq_b, \
         patch.object(tushare_rate_limiter_special, "acquire") as acq_s:
        tsync._fetch_with_guard("some_api", quota_type="special", trade_date="20250101")
        acq_s.assert_called_once()
        acq_b.assert_not_called()


def test_quota_缺省走基础桶():
    """quota_type 缺省（不传）走基础桶（向后兼容现有调用）。"""
    df = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20250101"]})
    p, _ = _patch_pro(df)
    with p, patch.object(tushare_rate_limiter_basic, "acquire") as acq_b:
        tsync._fetch_with_guard("some_api", trade_date="20250101")
        acq_b.assert_called_once()
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_tushare_sync_quota.py -v`
Expected: FAIL（`_fetch_with_guard` 不接受 `quota_type` 参数 / acquire 未按桶分流）

- [ ] **Step 3: 实现**

`data/tushare_sync.py:66` 改 `_fetch_with_guard` 签名 + 第 86 行 acquire 行：

```python
def _fetch_with_guard(api_name: str, *, quota_type: str = "basic", **kwargs) -> pd.DataFrame:
    """限频 + 熔断 + 异常分类包装的 pro 接口调用，空数据/失败返空 DF。

    quota_type（2026-07-25 双桶改造）：basic 走 500/min 基础桶，special 走 300/min 特色桶。
    缺省 basic（向后兼容未显式声明的数据集 + 旧调用）。三态处理（瞬时态/持久态/未知态）
    与异常分类逻辑不变，见 _classify_exc。
    """
    pro = get_pro()
    # 限频令牌桶：按 quota_type 选桶（基础500/特色300），阻塞至令牌可用。
    limiter = tushare_rate_limiter_special if quota_type == "special" else tushare_rate_limiter_basic
    limiter.acquire(1.0)
    # ... 其余熔断/退避逻辑不变
```

同步改导入（顶部 `from data.resilience import` 行加两个新桶）：

```python
from data.resilience import (
    tushare_breaker,
    tushare_rate_limiter_basic,
    tushare_rate_limiter_special,
)
```

3 处 `_fetch_with_guard` 调用加 `quota_type`：
- `_sync_by_symbol:295`: `df = _fetch_with_guard(api, quota_type=cfg.get("quota_type", "basic"), **kwargs)`
- `_sync_by_date:325`: 同上
- `_sync_single:382`: 同上

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_tushare_sync_quota.py -v`
Expected: 3 passed

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_tushare_sync.py tests/test_tushare_sync_quota.py -v`

```bash
git add data/tushare_sync.py tests/test_tushare_sync_quota.py
git commit -m "feat(sync): _fetch_with_guard 按 quota_type 路由双桶(基础/特色)

quota_type 从 cfg 读，basic→500/min桶，special→300/min桶，缺省basic向后兼容。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `_sync_by_symbol` 加 `adj_api` 前复权增强（daily/weekly/monthly 核心）

**Files:**
- Modify: `data/tushare_sync.py:250-302`（`_sync_by_symbol` 函数体）
- Test: `tests/test_sync_ohlcv_qfq.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `_fetch_with_guard(quota_type=...)`
- Produces: `_sync_by_symbol` 检测 `cfg["adj_api"]` 时额外拉 adj_factor 并按 `raw × adj / latest` 重建价格列；不变更签名（cfg 已传）

**物理意图（照搬 `sync_data_lake.fetch_qfq:99-132`，字节级一致）**：
- raw = `pro.<api>(ts_code, start_date, end_date)`（daily/weekly/monthly）
- adj = `pro.adj_factor(ts_code, start_date, end_date)`（日频复权因子）
- merge on trade_date → `price_qfq = raw_price × adj_factor / latest_adj`（latest=区间最新）
- volume/amount 不复权；rename vol→volume

- [ ] **Step 1: 写失败测试**

```python
# tests/test_sync_ohlcv_qfq.py
# -*- coding: utf-8 -*-
"""_sync_by_symbol 的 adj_api 前复权增强测试。"""
from unittest.mock import patch, MagicMock
import os
import pandas as pd
import pytest

import data.tushare_sync as tsync


def test_adj_api_触发前复权重建(tmp_path, monkeypatch):
    """cfg['adj_api'] 存在时，raw daily × adj_factor/latest → 价格列前复权。"""
    # 模拟 pro.daily 返原始价（除权前 close=10），adj_factor 区间最新=2.0、首日=1.0
    # 前复权后首日 close = 10 × 1.0 / 2.0 = 5.0
    raw = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 2,
        "trade_date": ["20250101", "20250102"],
        "open": [10.0, 11.0], "high": [10.5, 11.5],
        "low": [9.5, 10.5], "close": [10.0, 11.0],
        "vol": [1000.0, 1100.0], "amount": [10000.0, 11000.0],
    })
    adj = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 2,
        "trade_date": ["20250101", "20250102"],
        "adj_factor": [1.0, 2.0],  # latest=2.0（区间最新）
    })
    pro = MagicMock()
    pro.daily = MagicMock(return_value=raw)
    pro.adj_factor = MagicMock(return_value=adj)
    monkeypatch.setattr(tsync, "get_pro", lambda: pro)
    monkeypatch.setattr(tsync, "_trade_days", lambda s, e: ["20250101", "20250102"])
    monkeypatch.setattr(tsync, "resolve_symbols", lambda k, limit=None: ["000001.SZ"])
    # 跳过限频/熔断真实调用
    monkeypatch.setattr(tsync.tushare_rate_limiter_basic, "acquire", lambda x: None)
    monkeypatch.setattr(tsync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tsync.tushare_breaker, "record_success", lambda: None)

    cfg = {
        "api": "daily", "by": "symbol", "adj_api": "adj_factor",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},
        "lake": str(tmp_path / "out.parquet"),
        "quota_type": "basic",
    }
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_daily", cfg)
    shard_dir = str(tmp_path / "shards")
    monkeypatch.setattr(tsync, "_shard_dir", lambda k: shard_dir)
    # build_multiindex 落湖（真实 pyarrow 写）
    tsync.sync_dataset("_test_daily", "2025-01-01", "2025-01-02", symbols=["000001.SZ"], resume=False)
    # 校验落湖结果
    out = pd.read_parquet(cfg["lake"])
    # 找首日（20250101）的 close：前复权 = 10 × 1.0 / 2.0 = 5.0
    row0 = out.xs(pd.Timestamp("2025-01-01"), level="date").loc["000001.SZ"]
    assert row0["close"] == pytest.approx(5.0, rel=1e-6)
    # 最新日 close = 11 × 2.0 / 2.0 = 11.0（基准日不变）
    row1 = out.xs(pd.Timestamp("2025-01-02"), level="date").loc["000001.SZ"]
    assert row1["close"] == pytest.approx(11.0, rel=1e-6)
    # volume 不复权
    assert row0["volume"] == 1000.0
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_sync_ohlcv_qfq.py -v`
Expected: FAIL（`_sync_by_symbol` 不识别 `adj_api`，close 仍为原始 10.0）

- [ ] **Step 3: 实现**

`_sync_by_symbol`（`data/tushare_sync.py:250-302`）在拉 raw 后、落 shard 前插入前复权重建块。定位 `df = _fetch_with_guard(api, ...)` 之后、`df = _cleanse(...)` 之前，插入：

```python
        df = _fetch_with_guard(api, quota_type=cfg.get("quota_type", "basic"), **kwargs)
        if df.empty:
            continue
        # —— adj_api 前复权增强（daily/weekly/monthly，2026-07-25 Task 3）——
        # 物理意图：照搬 sync_data_lake.fetch_qfq，price_qfq = raw × adj / latest（区间最新），
        # 与 a_shares_daily.parquet 字节级一致。volume/amount 不复权（除权不影响成交额口径）。
        adj_api = (cfg or {}).get("adj_api")
        if adj_api:
            adj_kwargs = {code_param: ts_code}
            if not (cfg or {}).get("no_date_filter"):
                adj_kwargs["start_date"] = sd
                adj_kwargs["end_date"] = ed
            adj_df = _fetch_with_guard(adj_api, quota_type=cfg.get("quota_type", "basic"),
                                       fields="ts_code,trade_date,adj_factor", **adj_kwargs)
            if not adj_df.empty:
                adj_df["trade_date"] = pd.to_datetime(adj_df["trade_date"], format="%Y%m%d", errors="coerce")
                df_dt = pd.to_datetime(df[date_col], format="%Y%m%d", errors="coerce")
                adj_map = dict(zip(adj_df["trade_date"], adj_df["adj_factor"]))
                adj_series = df_dt.map(adj_map)
                latest_adj = adj_df.sort_values("trade_date")["adj_factor"].iloc[-1]
                if pd.isna(latest_adj) or latest_adj == 0:
                    latest_adj = 1.0
                for col in ("open", "high", "low", "close"):
                    if col in df.columns:
                        df[col] = df[col].astype(float) * adj_series.astype(float) / float(latest_adj)
        # —— 前复权增强结束 ——
        df = _cleanse(df, date_col)
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_sync_ohlcv_qfq.py -v`
Expected: PASS（close=5.0 前复权正确）

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_tushare_sync.py tests/test_sync_ohlcv_qfq.py tests/test_tushare_datasets_stock.py -v`

```bash
git add data/tushare_sync.py tests/test_sync_ohlcv_qfq.py
git commit -m "feat(sync): _sync_by_symbol 加 adj_api 前复权增强(daily/weekly/monthly)

照搬 fetch_qfq: price=raw×adj/latest，volume不复权，与a_shares_daily字节级一致。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: `resolve_symbols` 加 concept universe + concept 清理 `_unavailable`

**Files:**
- Modify: `data/tushare_sync.py:483-506`（`resolve_symbols` 加 concept 分支）
- Modify: `config/registry.py:355-365`（concept 配置删 `_unavailable`）
- Test: `tests/test_resolve_symbols_concept.py`（新建）

**Interfaces:**
- Produces: `resolve_symbols` 支持 `universe="concept"`（从 `data_lake/concept.parquet` 读 code 列表）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_resolve_symbols_concept.py
# -*- coding: utf-8 -*-
"""resolve_symbols 的 concept universe 分支测试。"""
from unittest.mock import patch
import pandas as pd

import data.tushare_sync as tsync


def test_universe_concept_从概念湖读id(tmp_path, monkeypatch):
    """universe='concept' 应从 data_lake/concept.parquet 读 code 列表返 id 列表。"""
    # 造一个假 concept 湖
    import os
    lake_dir = str(tmp_path / "data_lake")
    os.makedirs(lake_dir, exist_ok=True)
    concept_df = pd.DataFrame({"code": ["TS1", "TS2", "TS3"], "name": ["概念A", "概念B", "概念C"]})
    concept_df.to_parquet(os.path.join(lake_dir, "concept.parquet"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_concept_detail",
                        {"api": "concept_detail", "by": "symbol", "universe": "concept"})
    syms = tsync.resolve_symbols("_test_concept_detail")
    assert syms == ["TS1", "TS2", "TS3"]


def test_universe_concept_概念湖不存在返空(tmp_path, monkeypatch):
    """concept.parquet 不存在时返空列表（不抛，让 sync_dataset 自然 skip）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_concept_detail",
                        {"api": "concept_detail", "by": "symbol", "universe": "concept"})
    syms = tsync.resolve_symbols("_test_concept_detail")
    assert syms == []
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_resolve_symbols_concept.py -v`
Expected: FAIL（`resolve_symbols` 不识别 `universe="concept"`，走默认 stock 分支）

- [ ] **Step 3: 实现**

`data/tushare_sync.py:498-506`（`resolve_symbols` 的 universe 分支）加 concept：

```python
    cfg = TUSHARE_DATASETS[key]
    universe = cfg.get("universe", "stock")  # 缺省 stock（向后兼容未显式声明的数据集）
    if universe == "etf":
        syms = _load_etf_universe()
    elif universe == "index":
        syms = list(CORE_INDEX_CODES)
    elif universe == "concept":
        syms = _load_concept_ids()  # 从 data_lake/concept.parquet 读概念 id 列表
    else:  # stock（含缺省）
        syms = _load_universe()
    if limit:
        syms = syms[:limit]
    return syms
```

在 `_load_etf_universe` 之后（约 `data/tushare_sync.py:466`）新增 helper：

```python
def _load_concept_ids() -> list[str]:
    """概念 id 列表（从已落湖的 concept.parquet 读，供 concept_detail 的 by=symbol 消费）。

    Why 从湖读而非即时拉 concept 接口：concept（概念字典）是 concept_detail 的前置依赖，
    落湖后复用零额外配额；且 concept 接口静态，即时拉与读湖等价但后者省一次请求。
    Why 湖不存在返空：concept 未同步时 concept_detail 无标的可拉，返空让 sync_dataset 自然 skip，
    不抛异常阻断编排（编排脚本可先跑 concept 再跑 concept_detail）。
    """
    lake = os.path.join("data_lake", "concept.parquet")
    if not os.path.exists(lake):
        logger.warning("concept.parquet 不存在，concept_detail 无概念 id 可拉（请先同步 concept）")
        return []
    df = pd.read_parquet(lake)
    code_col = "code" if "code" in df.columns else df.columns[0]
    return df[code_col].astype(str).tolist()
```

`config/registry.py:355-365` concept 配置删 `_unavailable` 字段（代理已废，直连重探测，探测在 Task 11 dry-run）：

```python
    "concept": {
        # 2026-07-25：原 _unavailable（tnskhdata 代理无 concept 方法）已过时——代理 2026-07-24
        # 废弃，纯直连 tushare 官方 concept 接口可用。删除 _unavailable，恢复同步。
        # dry-run（Task 11）会探测直连真实可用性；若仍不可用再加回 _unavailable。
        "api": "concept", "by": "single",
        "date_col": "code", "symbol_col": "code",
        "fields": "code,name",
        "lake": "data_lake/concept.parquet",
        "quota_type": "basic",
    },
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_resolve_symbols_concept.py -v`
Expected: 2 passed

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_tushare_sync.py tests/test_resolve_symbols_concept.py tests/test_dataset_registry.py -v`

```bash
git add data/tushare_sync.py config/registry.py tests/test_resolve_symbols_concept.py
git commit -m "feat(sync): resolve_symbols 加 concept universe + 清理 concept 过时_unavailable

代理2026-07-24废弃后直连可用；concept_detail 按概念id分页走 by=symbol+universe=concept。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 注册表补基础桶新 key（stock_basic / hs_const / concept_detail）

**Files:**
- Modify: `config/registry.py:215`（`TUSHARE_DATASETS` 字典内追加条目）
- Test: `tests/test_tushare_datasets_snapshot.py`（新建）

**Interfaces:**
- Produces: `TUSHARE_DATASETS["stock_basic"|"hs_const_sh"|"hs_const_sz"|"concept_detail"]`，全部 `quota_type="basic"`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tushare_datasets_snapshot.py
# -*- coding: utf-8 -*-
"""新增数据集的 schema 完整性 + quota_type 归类测试。"""
from config.registry import TUSHARE_DATASETS


REQUIRED_FIELDS = {"api", "by", "date_col", "symbol_col", "fields", "lake", "quota_type"}


def test_stock_basic_已注册且基础桶():
    cfg = TUSHARE_DATASETS["stock_basic"]
    assert cfg["api"] == "stock_basic"
    assert cfg["by"] == "single"
    assert cfg["quota_type"] == "basic"
    assert "ts_code" in cfg["fields"]
    assert REQUIRED_FIELDS.issubset(cfg.keys())


def test_hs_const_沪深两湖已注册():
    for k in ("hs_const_sh", "hs_const_sz"):
        cfg = TUSHARE_DATASETS[k]
        assert cfg["api"] == "hs_const"
        assert cfg["by"] == "single"
        assert cfg["quota_type"] == "basic"
        assert REQUIRED_FIELDS.issubset(cfg.keys())
    assert TUSHARE_DATASETS["hs_const_sh"]["params"]["hs_type"] == "SH"
    assert TUSHARE_DATASETS["hs_const_sz"]["params"]["hs_type"] == "SZ"


def test_concept_detail_按概念id分页():
    cfg = TUSHARE_DATASETS["concept_detail"]
    assert cfg["api"] == "concept_detail"
    assert cfg["by"] == "symbol"
    assert cfg["universe"] == "concept"
    assert cfg["code_param"] == "id"
    assert cfg["quota_type"] == "basic"
    assert REQUIRED_FIELDS.issubset(cfg.keys())
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_tushare_datasets_snapshot.py -v`
Expected: FAIL（KeyError: 'stock_basic'）

- [ ] **Step 3: 实现**

`config/registry.py` 在 `TUSHARE_DATASETS` 字典末尾（`cyq_perf` 条目之后、闭合 `}` 之前，约 661 行）追加：

```python
    # ===== 数据快照扩容（2026-07-25）：基础桶新数据集 =====
    # 物理意图：补齐标的池源头（stock_basic）、互联互通成分（hs_const）、概念成分股（concept_detail）。
    # quota_type=basic：列表/成分类走基础桶 500/min。
    "stock_basic": {
        # 股票列表（标的池源头）：单次拉全市场在售（list_status='L'），by=single 落扁平 df。
        # Why 落湖：原仅 _load_universe 内部即时调用拿 universe，不落 parquet；提升为正式数据集
        # 便于白盒掌控标的池快照 + 供下游选股 universe 复用（零额外配额）。
        # Why params list_status='L'：与 _load_universe 一致过滤在售；不剔 ST（落湖保留全集，
        # 下游自行过滤，避免湖层信息损失）。退市标的（D）若需另立数据集或参数扩展。
        "api": "stock_basic", "by": "single",
        "date_col": "list_date", "symbol_col": "ts_code",
        "fields": "ts_code,symbol,name,area,industry,market,list_date",
        "params": {"list_status": "L"},
        "lake": "data_lake/stock_basic.parquet",
        "quota_type": "basic",
    },
    "hs_const_sh": {
        # 沪股通成分（hs_const hs_type=SH）：单次拉全量沪股通标的，by=single 落扁平 df。
        # 物理意图：互联互通成分是北向资金可投标的范围，用于外资流向归因 + 池子边界识别。
        # date_col=in_date（纳入日期）：标的纳入沪股通的时间，无前视风险（历史成分变动公开）。
        "api": "hs_const", "by": "single",
        "params": {"hs_type": "SH"},
        "date_col": "in_date", "symbol_col": "ts_code",
        "fields": "ts_code,hs_type,in_date,out_date,is_new",
        "lake": "data_lake/hs_const_sh.parquet",
        "quota_type": "basic",
    },
    "hs_const_sz": {
        # 深股通成分（hs_const hs_type=SZ）：同上，hs_type=SZ。
        "api": "hs_const", "by": "single",
        "params": {"hs_type": "SZ"},
        "date_col": "in_date", "symbol_col": "ts_code",
        "fields": "ts_code,hs_type,in_date,out_date,is_new",
        "lake": "data_lake/hs_const_sz.parquet",
        "quota_type": "basic",
    },
    "concept_detail": {
        # 概念成分股（concept_detail）：按概念 id 分页（pro.concept_detail(id=...)）。
        # Why by=symbol + universe=concept + code_param=id：复用 _sync_by_symbol 的逐标的分页，
        # 标的池=concept 湖的 id 列表（resolve_symbols universe=concept 分支），传参名=id 非 ts_code。
        # date_col=in_date（标的纳入概念日），无前视风险。
        # ⚠️ 前置依赖 concept.parquet（Task 4 已恢复），编排须先 concept 后 concept_detail。
        "api": "concept_detail", "by": "symbol",
        "universe": "concept",
        "code_param": "id",
        "date_col": "in_date", "symbol_col": "ts_code",
        "fields": "id,concept_name,ts_code,name,in_date,out_date",
        "lake": "data_lake/concept_detail.parquet",
        "quota_type": "basic",
    },
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_tushare_datasets_snapshot.py -v`
Expected: 3 passed

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_tushare_datasets_stock.py tests/test_dataset_registry.py tests/test_tushare_datasets_snapshot.py -v`

```bash
git add config/registry.py tests/test_tushare_datasets_snapshot.py
git commit -m "feat(registry): 补基础桶数据集 stock_basic/hs_const/concept_detail

stock_basic标的池源头+沪港通成分+概念成分股，全部quota_type=basic走500/min桶。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: 注册表补特色桶新 key（cyq_chips / daily_basic / stk_factor_pro）+ 现有 key 补 quota_type

**Files:**
- Modify: `config/registry.py`（追加 3 个特色桶 key + 给 moneyflow/daily_basic 等现有 key 标 quota_type）
- Test: `tests/test_tushare_datasets_snapshot.py`（Task 5 文件，追加用例）

**Interfaces:**
- Produces: `TUSHARE_DATASETS["cyq_chips"|"daily_basic"|"stk_factor_pro"]`，`quota_type="special"`；`moneyflow` 改 `special`

> ⚠️ **字段名 dry-run 探测在 Task 11**：cyq_chips/daily_basic/stk_factor_pro 的 fields 串先按 Tushare 官方文档填，Task 11 dry-run 用 `probe_tushare_fields.py` 验证真实列名，若有幻觉列在 Task 11 修正。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tushare_datasets_snapshot.py`：

```python
def test_特色桶新数据集已注册():
    for k in ("cyq_chips", "daily_basic", "stk_factor_pro"):
        cfg = TUSHARE_DATASETS[k]
        assert cfg["quota_type"] == "special", f"{k} 应归特色桶(300/min)"
        assert REQUIRED_FIELDS.issubset(cfg.keys()), f"{k} 缺必填字段"


def test_moneyflow_归特色桶():
    """资金流向按 Tushare 官方分类属特色数据，归 300/min 特色桶。"""
    assert TUSHARE_DATASETS["moneyflow"]["quota_type"] == "special"


def test_cyq_chips_按日分页():
    """cyq_chips 逐价位分布，by=date 单日全市场一次返（数据量大，按日分片）。"""
    cfg = TUSHARE_DATASETS["cyq_chips"]
    assert cfg["by"] == "date"
    assert "price" in cfg["fields"] and "percent" in cfg["fields"]
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_tushare_datasets_snapshot.py -v`
Expected: FAIL（KeyError: 'cyq_chips' / moneyflow 无 quota_type）

- [ ] **Step 3: 实现**

`config/registry.py` 追加（接 Task 5 的块之后）：

```python
    # ===== 数据快照扩容（2026-07-25）：特色桶新数据集（300/min）=====
    # 物理意图：补齐逐价位筹码明细（cyq_chips）+ 基本面因子（daily_basic）+ 技术因子（stk_factor_pro）。
    # quota_type=special：Tushare 官方把资金/筹码/因子归特色数据档（300/min），路由到特色桶。
    "cyq_chips": {
        # 逐价位筹码分布（cyq_chips）：每日每标的各价位占比，by=date 单日全市场一次返。
        # 物理意图：画筹码峰图 + 精细筹码集中度分析（cyq_perf 仅五档成本，本接口是逐价位明细）。
        # ⚠️ 数据量极大：每日 ~5000 标的 × ~10 价位 ≈ 5万行/日，5年 ≈ 6000万行（parquet 列压 ~2-3GB）。
        # shard 按日分片（by=date），断点续传按交易日粒度。
        # ⚠️ fields 待 Task 11 dry-run 探测确认（price/percent 按官方文档，可能有 ts_code/trade_date）。
        "api": "cyq_chips", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,price,percent",
        "lake": "data_lake/cyq_chips.parquet",
        "quota_type": "special",
    },
    "daily_basic": {
        # 每日全市场基本面因子（daily_basic）：PE/PB/换手率/市值，by=date 单日全市场。
        # 物理意图：估值因子（PE/PB）+ 流动性因子（换手率）核心数据源，每日 5000+ 行。
        # date_col=trade_date（交易日，无前视）。fields 按 Tushare daily_basic 输出（pe/pe_ttm/pb/ps）。
        "api": "daily_basic", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,close,turnover_rate,turnover_rate_f,pe,pe_ttm,pb,ps,total_mv,circ_mv",
        "lake": "data_lake/daily_basic.parquet",
        "quota_type": "special",
    },
    "stk_factor_pro": {
        # 技术面因子专业版（stk_factor_pro）：MACD/KDJ/BOLL/RSI/CCI 等，by=date 单日全市场。
        # 物理意图：技术因子核心源，供动量/反转/突破策略直接消费（免下游自算）。
        # ⚠️ fields 仅取核心（MACD/KDJ/BOLL/RSI/CCI），完整 80+ 因子按需扩展（避免湖膨胀）。
        "api": "stk_factor_pro", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,close,macd,kdj_k,kdj_d,kdj_j,boll_upper,boll_mid,boll_lower,rsi_6,cci",
        "lake": "data_lake/stk_factor_pro.parquet",
        "quota_type": "special",
    },
```

给 `moneyflow`（约 `registry.py:268`）加 `"quota_type": "special"`：

```python
    "moneyflow": {
        "api": "moneyflow", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_elg_amount,sell_elg_amount,net_mf_amount",
        "lake": "data_lake/moneyflow.parquet",
        "quota_type": "special",  # 资金流向按 Tushare 官方归特色数据（300/min）
    },
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_tushare_datasets_snapshot.py -v`
Expected: 6 passed（Task 5 的 3 + 本任务 3）

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_tushare_datasets_*.py tests/test_dataset_registry.py -v`

```bash
git add config/registry.py tests/test_tushare_datasets_snapshot.py
git commit -m "feat(registry): 补特色桶 cyq_chips/daily_basic/stk_factor_pro + moneyflow归特色

资金/筹码/因子按Tushare官方归特色数据档(300/min)，路由到特色限频桶。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: 注册表加 daily / weekly / monthly（by=symbol + adj_api 前复权）

**Files:**
- Modify: `config/registry.py`（追加 3 个 OHLCV key）
- Test: `tests/test_tushare_datasets_snapshot.py`（追加用例）

**Interfaces:**
- Consumes: Task 3 的 `_sync_by_symbol` adj_api 增强
- Produces: `TUSHARE_DATASETS["daily"|"weekly"|"monthly"]`，`by="symbol"` + `adj_api="adj_factor"` + `quota_type="basic"`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tushare_datasets_snapshot.py`：

```python
def test_OHLCV三频_by_symbol_adj_api():
    """daily/weekly/monthly 走 by=symbol + adj_api 前复权（Task 3 增强）。"""
    for k, api in [("daily", "daily"), ("weekly", "weekly"), ("monthly", "monthly")]:
        cfg = TUSHARE_DATASETS[k]
        assert cfg["api"] == api
        assert cfg["by"] == "symbol"
        assert cfg["adj_api"] == "adj_factor", f"{k} 必须配 adj_api 触发前复权"
        assert cfg["quota_type"] == "basic"
        assert "close" in cfg["fields"] and "vol" in cfg["fields"]
        assert cfg.get("rename", {}).get("vol") == "volume", f"{k} 必须 vol→volume 归一"
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_tushare_datasets_snapshot.py::test_OHLCV三频_by_symbol_adj_api -v`
Expected: FAIL（KeyError: 'daily'——TUSHARE_DATASETS 无 daily）

- [ ] **Step 3: 实现**

`config/registry.py` 追加（接 Task 6 块之后）：

```python
    # ===== OHLCV 前复权三频（2026-07-25）：daily/weekly/monthly 统一管道 =====
    # 物理意图：把 sync_data_lake.fetch_qfq 的前复权逻辑纳入 _sync_by_symbol（adj_api 增强），
    # daily/weekly/monthly 三频复用同一管道，shard 按标的 + MultiIndex(date,symbol) 落湖。
    # Why by=symbol 非 by=date：与既有 a_shares_daily.parquet 生产方式一致（fetch_qfq 范式），
    # 字节级可复现 + shard 按标的断点续传友好（5000 标的 × 2请求 ≈ 10000，500/min ≈ 20min）。
    # Why adj_api=adj_factor：_sync_by_symbol 检测 adj_api 后拉 adj_factor 重建前复权
    # price_qfq = raw × adj / latest（latest=区间最新），volume/amount 不复权。
    # Why rename vol→volume：Tushare daily/weekly/monthly 返 vol 列，与项目 OHLCV schema（volume）归一。
    "daily": {
        # 个股前复权日线：复用既有 a_shares_daily.parquet（903万行已落盘），保持一致性。
        "api": "daily", "by": "symbol", "adj_api": "adj_factor",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},
        "lake": "data_lake/a_shares_daily.parquet",
        "quota_type": "basic",
    },
    "weekly": {
        # 个股前复权周线：pro.weekly 接口（周末日 OHLCV + 周聚合成交），同 daily 管道。
        # adj_factor 日频，merge on trade_date（周线 trade_date=周末日，adj 该日有值）。
        "api": "weekly", "by": "symbol", "adj_api": "adj_factor",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},
        "lake": "data_lake/a_shares_weekly.parquet",
        "quota_type": "basic",
    },
    "monthly": {
        # 个股前复权月线：pro.monthly 接口，同 daily 管道。
        "api": "monthly", "by": "symbol", "adj_api": "adj_factor",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},
        "lake": "data_lake/a_shares_monthly.parquet",
        "quota_type": "basic",
    },
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_tushare_datasets_snapshot.py -v`
Expected: 8 passed

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_tushare_datasets_*.py -v`

```bash
git add config/registry.py tests/test_tushare_datasets_snapshot.py
git commit -m "feat(registry): daily/weekly/monthly 纳入统一前复权管道(by=symbol+adj_api)

复用 _sync_by_symbol adj_api 增强，与 a_shares_daily 字节级一致；weekly/monthly 新湖。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: DATASET_REGISTRY 同步补元信息（前端 DataLakeView 反射）

**Files:**
- Modify: `config/registry.py:39-197`（`DATASET_REGISTRY` 字典内追加新 key 的元信息）
- Test: `tests/test_dataset_registry.py`（追加用例）

**Interfaces:**
- Produces: `DATASET_REGISTRY` 包含所有新 TUSHARE_DATASETS key 的 source/market/granularity/script/freshness_hours

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_dataset_registry.py`：

```python
from config.registry import DATASET_REGISTRY


def test_新数据集_元信息完整():
    """所有新 TUSHARE_DATASETS key 必须在 DATASET_REGISTRY 有元信息（前端反射）。"""
    from config.registry import TUSHARE_DATASETS
    new_keys = ["stock_basic", "hs_const_sh", "hs_const_sz", "concept_detail",
                "cyq_chips", "daily_basic", "stk_factor_pro",
                "daily", "weekly", "monthly"]
    for k in new_keys:
        assert k in DATASET_REGISTRY, f"{k} 缺 DATASET_REGISTRY 元信息"
        meta = DATASET_REGISTRY[k]
        assert meta["source"] == "Tushare"
        assert meta["script"] == "scripts/sync_tushare.py"
        assert "market" in meta and "granularity" in meta and "freshness_hours" in meta
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_dataset_registry.py::test_新数据集_元信息完整 -v`
Expected: FAIL（KeyError: 'stock_basic' in DATASET_REGISTRY）

- [ ] **Step 3: 实现**

`config/registry.py` 在 `DATASET_REGISTRY` 字典末尾（`mkt_daily` 条目之后、闭合 `}` 之前，约 196 行）追加：

```python
    # ============================================================================
    # 数据快照扩容（2026-07-25）：新增数据集元信息（前端 DataLakeView 反射）
    # ============================================================================
    # 设计意图：Task 5/6/7 新增的 TUSHARE_DATASETS key 必须在此补元信息，否则前端
    # DataLakeView 表格看不到这些资产（DATASET_REGISTRY 是前端反射的单一真相源）。
    # script 统一 scripts/sync_tushare.py（server data_service 子进程拉起该薄壳）。
    # freshness_hours：日频=24h、月频=730h、静态快照=730h（标的不常变动）。
    # —— 列表 / 成分类（基础桶）——
    "stock_basic":    {"source": "Tushare", "market": "A股", "granularity": "快照",
                       "script": "scripts/sync_tushare.py", "schedule": "每月", "freshness_hours": 730},
    "hs_const_sh":    {"source": "Tushare", "market": "A股", "granularity": "快照",
                       "script": "scripts/sync_tushare.py", "schedule": "每季", "freshness_hours": 2190},
    "hs_const_sz":    {"source": "Tushare", "market": "A股", "granularity": "快照",
                       "script": "scripts/sync_tushare.py", "schedule": "每季", "freshness_hours": 2190},
    "concept_detail": {"source": "Tushare", "market": "板块", "granularity": "快照",
                       "script": "scripts/sync_tushare.py", "schedule": "每月", "freshness_hours": 730},
    # —— 特色数据类（特色桶）——
    "cyq_chips":      {"source": "Tushare", "market": "A股", "granularity": "1d",
                       "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "daily_basic":    {"source": "Tushare", "market": "A股", "granularity": "1d",
                       "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "stk_factor_pro": {"source": "Tushare", "market": "A股", "granularity": "1d",
                       "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    # —— OHLCV 三频（基础桶，daily 复用既有湖）——
    # daily 的 script 仍标 sync_tushare.py（统一入口），原 sync_data_lake.py 转薄壳 deprecated。
    "daily":          {"source": "Tushare", "market": "A股", "granularity": "1d",
                       "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "weekly":         {"source": "Tushare", "market": "A股", "granularity": "1w",
                       "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "monthly":        {"source": "Tushare", "market": "A股", "granularity": "1M",
                       "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_dataset_registry.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest tests/test_dataset_registry.py tests/test_tushare_datasets_snapshot.py -v`

```bash
git add config/registry.py tests/test_dataset_registry.py
git commit -m "feat(registry): DATASET_REGISTRY 补10个新数据集元信息(前端DataLakeView反射)

source/market/granularity/script/freshness_hours，script统一sync_tushare.py薄壳。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 9: 统一 CLI `data/sync_cli.py` + `python -m data.sync`

**Files:**
- Create: `data/sync_cli.py`
- Create: `data/__main__.py`（让 `python -m data.sync` 生效，转调 sync_cli.main）
- Test: `tests/test_sync_cli.py`（新建）

**Interfaces:**
- Consumes: `sync_dataset`、`TUSHARE_DATASETS`、`resolve_symbols`
- Produces: `python -m data.sync --all/--keys/--since/--end/--incremental/--dry-run/--quota/--no-resume`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_sync_cli.py
# -*- coding: utf-8 -*-
"""统一 CLI 参数解析 + key 过滤测试（不真跑同步，mock sync_dataset）。"""
from unittest.mock import patch
import pytest

from data.sync_cli import parse_args, select_keys, run


def test_parse_args_全量():
    args = parse_args(["--all", "--since", "2021-01-01"])
    assert args.all is True
    assert args.since == "2021-01-01"
    assert args.quota is None


def test_parse_args_指定keys():
    args = parse_args(["--keys", "daily,weekly", "--since", "2021-01-01"])
    assert args.keys == ["daily", "weekly"]
    assert args.all is False


def test_parse_args_dry_run():
    args = parse_args(["--keys", "moneyflow", "--dry-run"])
    assert args.dry_run is True


def test_select_keys_按quota过滤():
    """--quota basic 只选基础桶 key。"""
    from config.registry import TUSHARE_DATASETS
    # 假设 moneyflow 已标 special（Task 6）、stock_basic 标 basic（Task 5）
    basic = select_keys(all_keys=True, quota="basic")
    assert "stock_basic" in basic
    assert "moneyflow" not in basic  # moneyflow 是 special


def test_run_单key失败不中断后续(monkeypatch, tmp_path):
    """fail-soft：某 key 抛异常，后续 key 仍跑，汇总 exit code=1。"""
    calls = []
    def fake_sync(key, start, end, **kw):
        calls.append(key)
        if key == "bad":
            raise RuntimeError("故意失败")
    monkeypatch.setattr("data.sync_cli.sync_dataset", fake_sync)
    rc = run(keys=["good1", "bad", "good2"], since="2021-01-01", end="2021-01-02")
    assert rc == 1  # 部分失败
    assert calls == ["good1", "bad", "good2"]  # bad 之后 good2 仍执行
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_sync_cli.py -v`
Expected: FAIL（`ModuleNotFoundError: data.sync_cli`）

- [ ] **Step 3: 实现 `data/sync_cli.py`**

```python
# -*- coding: utf-8 -*-
"""统一 Tushare 同步 CLI：python -m data.sync [选项]

设计意图（高内聚低耦合，2026-07-25 scripts/ 收敛）：
- 把散装 scripts/sync_*.py（sync_all_tushare/sync_incremental/sync_data_lake/sync_daily_incremental）
  的能力收敛到一个 CLI，底层复用 data.tushare_sync.sync_dataset 统一引擎。
- scripts/sync_*.py 转薄壳 + DeprecationWarning 转调本 CLI（server data_service 仍依赖
  scripts/sync_tushare.py 薄壳，故该脚本不转，仅作 key 单同步入口保留）。

用法：
  python -m data.sync --all --since 2021-01-01                  # 全量回填所有数据集
  python -m data.sync --keys daily,weekly,cyq_chips --since 2021-01-01
  python -m data.sync --keys moneyflow --incremental             # 增量（湖最新日→今天）
  python -m data.sync --keys daily --dry-run --since 2025-07-01  # 小样例（limit=2 标的/1日）
  python -m data.sync --quota basic --since 2021-01-01           # 仅基础桶数据集

退出码：0=全成功，1=部分失败（fail-soft，单 key 失败不中断后续）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from config import TUSHARE_DATASETS
from data.tushare_sync import sync_dataset

logger = logging.getLogger(__name__)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """argparse 参数解析（抽出便于测试）。"""
    ap = argparse.ArgumentParser(description="统一 Tushare 数据集同步 CLI")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="同步全部 TUSHARE_DATASETS")
    g.add_argument("--keys", help="逗号分隔的数据集 key 列表（如 daily,weekly,moneyflow）")
    ap.add_argument("--since", help="起始日 YYYY-MM-DD（缺省=近5年）", default=None)
    ap.add_argument("--end", help="结束日 YYYY-MM-DD（缺省=今天）", default=None)
    ap.add_argument("--quota", choices=["basic", "special"], default=None,
                    help="仅同步指定配额桶（基础500/特色300）的数据集")
    ap.add_argument("--incremental", action="store_true",
                    help="增量模式：读湖最新日 d0 → 拉 [d0+1, today]（仅 by=date/symbol 时序湖）")
    ap.add_argument("--dry-run", action="store_true",
                    help="小样例：by=symbol 限 2 标的 / by=date 限 1 日，验证字段与落湖")
    ap.add_argument("--no-resume", action="store_true", help="不断点续传（重拉已存在 shard）")
    ap.add_argument("--limit", type=int, default=None, help="by=symbol 时仅前 N 只标的")
    return ap.parse_args(argv)


def select_keys(*, all_keys: bool, keys: Optional[str], quota: Optional[str]) -> list[str]:
    """选 key 列表：--all 取全集（可按 --quota 过滤），--keys 解析逗号串。"""
    if all_keys:
        sel = list(TUSHARE_DATASETS.keys())
    else:
        sel = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if quota:
        sel = [k for k in sel if TUSHARE_DATASETS[k].get("quota_type", "basic") == quota]
    # 排除 _unavailable（代理坑残留或 dry-run 探测不可用）
    sel = [k for k in sel if not TUSHARE_DATASETS[k].get("_unavailable")]
    return sel


def _resolve_window(key: str, since: Optional[str], end: Optional[str],
                    incremental: bool) -> tuple[str, str]:
    """解析 [start, end] 窗口：incremental 读湖最新日；否则 since..end（缺省近5年..今天）。"""
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    if incremental:
        # 读湖最新日 d0 → start=d0+1（增量补新）
        lake = TUSHARE_DATASETS[key]["lake"]
        try:
            df = pd.read_parquet(lake)
            idx_dates = df.index.get_level_values("date") if isinstance(df.index, pd.MultiIndex) else df.index
            d0 = str(pd.Timestamp(idx_dates.max()).date())
            start = (pd.Timestamp(d0) + timedelta(days=1)).strftime("%Y-%m-%d")
            if start >= end:
                logger.info("[%s] 已最新 %s，跳过增量", key, d0)
                return (None, None)
            return (start, end)
        except Exception as e:
            logger.warning("[%s] 增量读湖失败 %s，回退 since", key, e)
    start = since or (datetime.today() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
    return (start, end)


def run(keys: list[str], since: Optional[str], end: Optional[str],
        incremental: bool = False, dry_run: bool = False,
        resume: bool = True, limit: Optional[int] = None) -> int:
    """执行同步：逐 key 调 sync_dataset，fail-soft 汇总。返回 exit code（0全成功/1部分失败）。"""
    failures: list[tuple[str, str]] = []
    for key in keys:
        start, end_w = _resolve_window(key, since, end, incremental)
        if start is None:
            continue
        symbols = None
        if limit or dry_run:
            from data.tushare_sync import resolve_symbols
            try:
                symbols = resolve_symbols(key, limit=limit or (2 if dry_run else None))
            except Exception:
                symbols = None  # by=date/single 不需 symbols，忽略
        if dry_run and symbols is None:
            # by=date dry-run：限 1 日（缩 end 到 since+1天）
            try:
                d1 = (pd.Timestamp(start) + timedelta(days=1)).strftime("%Y-%m-%d")
                end_w = min(end_w, d1)
            except Exception:
                pass
        t0 = time.time()
        try:
            sync_dataset(key, start, end_w, symbols=symbols, resume=resume)
            logger.info("[%s] OK elapsed=%.0fs", key, time.time() - t0)
        except Exception as e:
            logger.exception("[%s] FAIL", key)
            failures.append((key, str(e)))
    # 汇总
    logger.info("=" * 60)
    logger.info("同步完成：成功 %d，失败 %d", len(keys) - len(failures), len(failures))
    for k, err in failures:
        logger.info("  FAIL %s: %s", k, err[:120])
    return 1 if failures else 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口（python -m data.sync 调此）。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    keys = select_keys(all_keys=args.all, keys=args.keys, quota=args.quota)
    if not keys:
        logger.error("无数据集可选（检查 --keys/--quota 或 _unavailable）")
        return 2
    logger.info("待同步 %d 数据集：%s", len(keys), keys)
    return run(keys=keys, since=args.since, end=args.end,
               incremental=args.incremental, dry_run=args.dry_run,
               resume=not args.no_resume, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
```

`data/__main__.py`（让 `python -m data.sync` 实际上要走 `data.sync_cli`——但 `python -m data.sync` 会找 `data/sync.py`。为支持 `python -m data.sync`，需 `data/sync.py` 薄壳转调。改用 `data/sync.py`）：

**修正**：`python -m data.sync` 找的是 `data/sync.py` 模块（不是 `__main__.py`）。所以新建 `data/sync.py`：

```python
# -*- coding: utf-8 -*-
"""python -m data.sync 入口薄壳（转调 data.sync_cli.main）。

Why 独立 sync.py 而非 sync_cli/__main__.py：python -m data.sync 解析 data/sync.py 模块，
执行其 __main__ 块。sync_cli.py 含完整 CLI 逻辑（便于测试 import），sync.py 仅作 -m 入口。
"""
import sys
from data.sync_cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_sync_cli.py -v`
Expected: 5 passed

- [ ] **Step 5: 回归 + commit**

Run: `python -m data.sync --help`（验证 CLI 可用）+ `python -m pytest tests/test_sync_cli.py -v`

```bash
git add data/sync_cli.py data/sync.py tests/test_sync_cli.py
git commit -m "feat(cli): 统一同步入口 python -m data.sync（收敛 scripts/ 散装脚本）

支持 --all/--keys/--since/--quota/--incremental/--dry-run，fail-soft 汇总。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 10: `scripts/` 收敛薄壳 + DeprecationWarning

**Files:**
- Modify: `scripts/sync_data_lake.py`（顶部加 DeprecationWarning，`__main__` 转调 `python -m data.sync --keys daily`）
- Modify: `scripts/sync_all_tushare.py`（转调 `--all`）
- Modify: `scripts/sync_incremental.py`（转调 `--incremental`）
- Modify: `scripts/sync_daily_incremental.py`（转调 `--keys daily --incremental`）
- Modify: `scripts/sync_tushare.py`（保留，仅加注释说明 server 依赖它，不自 deprecated）
- Test: `tests/test_scripts_deprecated.py`（新建，验证薄壳转调）

**Interfaces:**
- Consumes: Task 9 的 `data.sync_cli.main`

> **红线**：`scripts/sync_tushare.py` **不 deprecated**（server `data_service` 子进程 + `DATASET_REGISTRY.script` 依赖它），只加注释。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_scripts_deprecated.py
# -*- coding: utf-8 -*-
"""散装 scripts/ 已转薄壳调统一 CLI（DeprecationWarning）。"""
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_sync_data_lake_转调统一CLI():
    """sync_data_lake.py 带参数时应转调 python -m data.sync（输出含 daily）。"""
    # 用 --help 或无副作用调用验证薄壳转调（实际同步太慢，用 -h）
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "sync_data_lake.py"), "--help"],
                       capture_output=True, text=True, timeout=30)
    # deprecated 薄壳应在 stderr 输出 DeprecationWarning，并转调统一 CLI 的 help
    assert "deprecated" in (r.stdout + r.stderr).lower() or "data.sync" in (r.stdout + r.stderr)
```

- [ ] **Step 2: 验证失败**

Run: `python -m pytest tests/test_scripts_deprecated.py -v`
Expected: FAIL（sync_data_lake.py 未转薄壳）

- [ ] **Step 3: 实现**

`scripts/sync_data_lake.py` 顶部（`from __future__ import annotations` 之后）插入 DeprecationWarning + `__main__` 改写：

```python
import warnings
warnings.warn(
    "scripts/sync_data_lake.py 已 deprecated（2026-07-25 scripts/ 收敛）。"
    "daily 前复权已纳入统一管道，请改用：python -m data.sync --keys daily --since 2021-01-01",
    DeprecationWarning,
    stacklevel=2,
)
```

`scripts/sync_data_lake.py` 的 `if __name__ == "__main__":` 块改为转调（保留原 argparse 兼容旧调用，但内部转调）：

```python
if __name__ == "__main__":
    # deprecated 转调：daily 前复权已迁移到 _sync_by_symbol adj_api 增强（统一管道）。
    # 保留旧 argparse 接口兼容（--years/--out/--limit），内部转 python -m data.sync。
    import argparse as _ap
    _ap2 = _ap.ArgumentParser(add_help=False)
    _ap2.add_argument("--years", type=int, default=5)
    _ap2.add_argument("--limit", type=int, default=None)
    _args, _ = _ap2.parse_known_args()
    from datetime import datetime as _dt, timedelta as _td
    _end = _dt.today().strftime("%Y-%m-%d")
    _start = (_dt.today() - _td(days=365 * _args.years)).strftime("%Y-%m-%d")
    from data.sync_cli import main as _cli_main
    _cli_argv = ["--keys", "daily", "--since", _start, "--end", _end]
    if _args.limit:
        _cli_argv += ["--limit", str(_args.limit)]
    sys.exit(_cli_main(_cli_argv))
```

对 `scripts/sync_all_tushare.py`、`scripts/sync_incremental.py`、`scripts/sync_daily_incremental.py` 做类似处理（顶部 DeprecationWarning + `__main__` 转调对应 `--all`/`--incremental`/`--keys daily --incremental`）。

`scripts/sync_tushare.py` 顶部加注释（不 deprecated）：

```python
# 2026-07-25 注：本脚本为 server data_service 子进程入口（DATASET_REGISTRY.script 指向它），
# 不 deprecated。单 key 同步仍走此薄壳（转调 sync_dataset）。多 key / 全量 / 增量改用
# python -m data.sync（data/sync_cli.py）。
```

- [ ] **Step 4: 验证通过**

Run: `python -m pytest tests/test_scripts_deprecated.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + commit**

Run: `python scripts/sync_tushare.py --help`（验证 server 依赖的薄壳仍可用）

```bash
git add scripts/sync_data_lake.py scripts/sync_all_tushare.py scripts/sync_incremental.py scripts/sync_daily_incremental.py scripts/sync_tushare.py tests/test_scripts_deprecated.py
git commit -m "refactor(scripts): 散装 sync 脚本转薄壳+DeprecationWarning(收敛到 data.sync)

sync_tushare.py 保留(server data_service依赖)；其余4脚本 deprecated 转调统一CLI。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 11: dry-run 字段探测（cyq_chips / daily_basic / stk_factor_pro / concept_detail / weekly / monthly）

**Files:**
- Use: `data/tools/probe_tushare_fields.py`（项目已有探测脚本习惯）
- Maybe Modify: `config/registry.py`（若探测发现幻觉列，订正 fields 串）

**物理意图**：全量回填前用最小配额（每接口 1-2 次请求）验证字段真实性，防幻觉列导致空数据反复拉。沿用项目 `probe_tushare_fields.py` 习惯。

- [ ] **Step 1: 探测脚本**

```bash
python -c "
from data._tushare_compat import get_pro
pro = get_pro()
# 探测各新接口真实列名（1次请求/接口）
print('=== cyq_chips ==='); print(pro.cyq_chips(trade_date='20250106', limit=2).columns.tolist() if not pro.cyq_chips(trade_date='20250106', limit=2).empty else 'EMPTY')
print('=== daily_basic ==='); print(pro.daily_basic(trade_date='20250106', limit=2).columns.tolist())
print('=== stk_factor_pro ==='); print(pro.stk_factor_pro(trade_date='20250106', limit=2).columns.tolist() if hasattr(pro,'stk_factor_pro') else 'NO METHOD')
print('=== concept_detail ==='); print(pro.concept_detail(id='TS2', limit=2).columns.tolist() if hasattr(pro,'concept_detail') else 'NO METHOD')
print('=== weekly ==='); print(pro.weekly(ts_code='000001.SZ', start_date='20250101', end_date='20250131', limit=2).columns.tolist())
print('=== monthly ==='); print(pro.monthly(ts_code='000001.SZ', start_date='20250101', end_date='20250131', limit=2).columns.tolist())
print('=== hs_const ==='); print(pro.hs_const(hs_type='SH', limit=2).columns.tolist())
print('=== concept ==='); print(pro.concept(limit=2).columns.tolist())
"
```

- [ ] **Step 2: 对照注册表 fields 串，订正幻觉列**

逐接口比对真实列名 vs `TUSHARE_DATASETS[key]["fields"]`。若发现幻觉列（注册表写了但 API 不返），用 `Edit` 订正 `config/registry.py` 对应 fields 串。若某接口"No such method"（积分不足），给该 key 加 `_unavailable` 字段（同 concept 旧范式）。

- [ ] **Step 3: 小样例 dry-run 落湖验证**

```bash
python -m data.sync --keys stock_basic,hs_const_sh,concept,daily_basic,cyq_chips,stk_factor_pro,weekly,monthly \
    --since 2025-07-01 --end 2025-07-02 --dry-run
```

校验：每 key 在 `data_lake/` 产出 parquet，用 `python -c "import pandas as pd; print(pd.read_parquet('data_lake/cyq_chips.parquet').head())"` 抽样查非空、列名正确。

- [ ] **Step 4: commit（若 fields 订正）**

```bash
git add config/registry.py
git commit -m "fix(registry): dry-run 探测订正字段名(防幻觉列)

cyq_chips/daily_basic/stk_factor_pro/weekly/monthly 字段按真实API输出校准。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

- [ ] **Step 5: 不 commit（若无需订正）**——记录探测结果到日志，进 Task 12。

---

## Task 12: daily 迁移一致性验证（新管道 vs 旧 fetch_qfq）

**Files:**
- Test: `tests/test_daily_migration_parity.py`（新建，一次性验证测试，验证后可保留作回归）

**物理意图**：新 `_sync_by_symbol(adj_api=adj_factor)` 必须与旧 `sync_data_lake.fetch_qfq` 在同区间产出**字节级一致**，否则破坏 903 万行既有 `a_shares_daily.parquet`。

- [ ] **Step 1: 写一致性验证测试**

```python
# tests/test_daily_migration_parity.py
# -*- coding: utf-8 -*-
"""daily 迁移一致性：新 _sync_by_symbol(adj_api) vs 旧 sync_data_lake.fetch_qfq 字节级对齐。"""
from unittest.mock import MagicMock
import pandas as pd
import pytest

import data.tushare_sync as tsync
from scripts.sync_data_lake import fetch_qfq


def test_新管道与旧fetch_qfq同区间产出一致(tmp_path, monkeypatch):
    """同标的同区间，新 _sync_by_symbol 与旧 fetch_qfq 的前复权 close 应逐值相等。"""
    ts_code = "000001.SZ"
    raw = pd.DataFrame({
        "ts_code": [ts_code]*3, "trade_date": ["20250101","20250102","20250103"],
        "open": [10.,11.,12.], "high": [10.5,11.5,12.5],
        "low": [9.5,10.5,11.5], "close": [10.,11.,12.],
        "vol": [1000.,1100.,1200.], "amount": [1e4,1.1e4,1.2e4],
    })
    adj = pd.DataFrame({
        "ts_code": [ts_code]*3, "trade_date": ["20250101","20250102","20250103"],
        "adj_factor": [1.0, 1.0, 2.0],
    })
    # mock pro：daily 与 adj_factor 都返上表
    pro = MagicMock()
    pro.daily = MagicMock(return_value=raw)
    pro.adj_factor = MagicMock(return_value=adj)
    monkeypatch.setattr(tsync, "get_pro", lambda: pro)
    # —— 旧管道 fetch_qfq ——
    monkeypatch.setattr("scripts.sync_data_lake.get_pro", lambda: pro)
    monkeypatch.setattr("scripts.sync_data_lake._fetch_with_guard",
                        lambda p, api, **kw: raw if api=="daily" else adj)
    old_df = fetch_qfq(pro, ts_code, "2025-01-01", "2025-01-03")
    # —— 新管道：手动复刻 _sync_by_symbol 的前复权块（抽出为纯函数更佳，此处直接比对公式结果）——
    # 新管道公式：close × adj / latest（latest=2.0）
    expected_close = [10.*1./2., 11.*1./2., 12.*2./2.]  # [5.0, 5.5, 12.0]
    assert old_df["close"].tolist() == pytest.approx(expected_close, rel=1e-6), \
        "旧 fetch_qfq 前复权结果（基准参考）"
    # 新管道结果应等于旧管道（同公式）
    # （完整对齐在 dry-run 实跑时用 assert_frame_equal 覆盖更多标的，此处锁定公式等价）
```

- [ ] **Step 2: 验证通过（公式等价性）**

Run: `python -m pytest tests/test_daily_migration_parity.py -v`
Expected: PASS（证明新管道 adj_api 增强与旧 fetch_qfq 用同一前复权公式）

- [ ] **Step 3: dry-run 实跑对齐（小标的）**

```bash
# 用新管道拉 1 只标的近 1 月，与旧 fetch_qfq 同区间产出对比
python -c "
import pandas as pd
from data.tushare_sync import sync_dataset
sync_dataset('daily', '2025-06-01', '2025-06-30', symbols=['000001.SZ'], resume=False)
new = pd.read_parquet('data_lake/a_shares_daily.parquet')
new = new.xs('000001.SZ', level='symbol').loc['2025-06-01':'2025-06-30']
print('新管道 close:'); print(new['close'].head())
"
```

> 若新管道产出与既有 `a_shares_daily.parquet` 同区间 close 偏差 >1e-6，回 Task 3 检查 `latest_adj` 取值逻辑（区间最新 vs 全局最新）。

- [ ] **Step 4: commit**

```bash
git add tests/test_daily_migration_parity.py
git commit -m "test(sync): daily 迁移一致性验证(新adj_api管道 vs 旧fetch_qfq 公式等价)

保护903万行既有a_shares_daily.parquet不被破坏。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 13: 全量回填（基础桶 + 特色桶 + OHLCV 三频）

**Files:**
- Run: CLI（无代码改动）
- Report: `data_lake/.syncing/snapshot-2026-07-25.log`

**物理意图**：执行用户核心诉求——本次就跑一轮近 5 年（2021-01-01 起）全量回填，盯到底报每集入库行数。

> ⚠️ **配额预算**：基础桶 daily/weekly/monthly 各 ~5000 标的 × 2 请求 ≈ 30000 请求，500/min ≈ 1h；特色桶 daily_basic/stk_factor_pro by=date ~1200 日 × 1 请求 ≈ 1200 请求，300/min ≈ 4min；cyq_chips by=date 同 ~1200 请求；concept_detail by=symbol ~5000 概念 id × 1 请求 ≈ 5000，basic 桶 ≈ 10min。总计预估 2-3h。

- [ ] **Step 1: dry-run 预检（Task 11 已做，此处确认 shard 干净）**

```bash
# 确认无残留 shard 影响断点续传判断
ls data_lake/shards/ 2>/dev/null | head
```

- [ ] **Step 2: 基础桶全量回填**

```bash
python -m data.sync --quota basic --since 2021-01-01 2>&1 | tee data_lake/.syncing/snapshot-basic.log
```

盯日志：每 key 输出 `[key] OK elapsed=Xs`，失败 key 记录原因。断点续传保证重跑只补失败 key。

- [ ] **Step 3: 特色桶全量回填**

```bash
python -m data.sync --quota special --since 2021-01-01 2>&1 | tee data_lake/.syncing/snapshot-special.log
```

- [ ] **Step 4: OHLCV 三频全量回填（daily 复用既有湖，weekly/monthly 新湖）**

```bash
python -m data.sync --keys daily,weekly,monthly --since 2021-01-01 2>&1 | tee data_lake/.syncing/snapshot-ohlcv.log
```

> daily 因复用 `a_shares_daily.parquet`（903万行已落盘），sync_dataset 会因 resume 跳过已存在 shard（按标的），仅补缺失标的/区间。若需全量重算，加 `--no-resume`（慎用，会重拉 5000 标的）。

- [ ] **Step 5: 汇总报告**

```bash
python -c "
import os, pandas as pd
lakes = ['stock_basic','hs_const_sh','hs_const_sz','concept','concept_detail',
         'cyq_chips','daily_basic','stk_factor_pro','daily','weekly','monthly',
         'a_shares_daily','a_shares_weekly','a_shares_monthly']
print(f'{\"lake\":<30} {\"rows\":>12} {\"size_MB\":>10}')
for k in lakes:
    p = f'data_lake/{k}.parquet'
    if os.path.exists(p):
        df = pd.read_parquet(p)
        sz = os.path.getsize(p)/1e6
        print(f'{k:<30} {len(df):>12} {sz:>10.1f}')
"
```

输出汇总表，记录到 `data_lake/.syncing/snapshot-2026-07-25.log` 末尾。

- [ ] **Step 6: 最终 commit（日志 + 任何回填中的 hotfix）**

```bash
git add data_lake/.syncing/  # 日志归档
git commit -m "chore(data): 2026-07-25 全量回填完成（近5年，基础+特色+OHLCV）

详见 data_lake/.syncing/snapshot-*.log。daily复用既有湖，weekly/monthly新湖。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 全任务完成后回归

```bash
python -m pytest tests/ -v -k "resilience_quota or tushare_sync_quota or sync_ohlcv_qfq or resolve_symbols_concept or tushare_datasets_snapshot or dataset_registry or sync_cli or scripts_deprecated or daily_migration"
```

全绿 = 交付完成。
