# T13-A · L1 写入侧安全网 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给数据湖所有写入口装上「写入前历史行数守卫」并收口 daily 双轨、给 freshness 加行数骤降维度——封死 T12 式残片覆盖（1020万→3200）的静默抹除路径。

**Architecture:** 在 `data/integrity.py` 落「行数检查 SSoT」纯函数（`check_row_count_drop` + `existing_row_count`）+ 编排器 `assert_safe_overwrite`（拒写抛 `WriteGuardError` + CRITICAL 告警）。四个湖写入口（通用同步器 `_sync_single`/`_build_multiindex`、`sync_daily_incremental`、`repair_gaps`）落盘前统一调用。daily 双轨收口靠删 `TUSHARE_DATASETS["daily"]` + 改 `DATASET_REGISTRY["daily"]["script"]` 指向增量脚本。freshness 行数骤降复用同一 `check_row_count_drop` + sidecar 基线。

**Tech Stack:** Python 3.10 · pandas · pyarrow（parquet 元数据读行数，免全量读）· pytest。

## Global Constraints

- **全中文注释**（CLAUDE.md）：所有新增代码配「What + Why」中文注释，复杂处说明防范的极端行情/数据边界。
- **写入守卫 FAIL = 硬阻断**：拒写抛 `WriteGuardError` + `logger.critical`，绝不静默放行；破坏性覆盖必须在守卫前被拦下。
- **行数检查 SSoT**（蓝图 §5 原则 2）：行数比较逻辑只许存在于 `data/integrity.py` 的 `check_row_count_drop`；freshness 与写入守卫都调它，禁止两套实现。
- **写入守卫普适**（蓝图 §5 原则 3）：四个湖写入口全部接入，一个守卫复用。
- **不破坏既有行为**：`sync_daily_incremental` 的增量 append 语义不变；`weekly`/`monthly` 仍走 `sync_tushare.py`（只删 `daily`，不动周月）。
- **逃生口**：`QUANTER_FORCE_WRITE=1` 环境变量旁路守卫（用于人为故意缩小重采），但旁路仍 `logger.critical` 留痕——守卫可拒绝、可强旁、不可静默。
- **阈值默认 0.9**：`WRITE_GUARD_MIN_RATIO = 0.9`（新行数 < 现有 × 0.9 → 拒写）。蓝图级默认，可在调用点覆盖。
- **行号时效**：本计划行号基于 2026-08-10 工作区；实现时若代码已漂移，按函数名/注释定位，不要盲信行号。

---

## File Structure

| 文件 | 责任 | 本计划动作 |
|---|---|---|
| `data/integrity.py` | 行数检查 SSoT + 写入守卫 | 新增 `WRITE_GUARD_MIN_RATIO`/`WriteGuardError`/`existing_row_count`/`check_row_count_drop`/`assert_safe_overwrite` |
| `data/tushare_sync.py` | 通用同步器（全量覆盖写） | `_sync_single`(:525)、`_build_multiindex`(:231) 落盘前接入守卫 |
| `data/tools/sync_daily_incremental.py` | daily 增量同步（append） | `:244` 落盘前接入守卫 |
| `data/tools/repair_gaps.py` | 补采（重写湖） | `:172` 落盘前接入守卫 |
| `config/registry.py` | 数据集注册表（双轨源头） | 删 `TUSHARE_DATASETS["daily"]`(:707)；`DATASET_REGISTRY["daily"]["script"]`(:48) 指向增量脚本 |
| `data/freshness.py` | 实时性 gate | `FreshnessResult` 加 `row_count`；`check_freshness` 加行数骤降检测（sidecar 基线） |
| `tests/test_integrity.py` | 守卫单元测试 | 新增守卫测试 |
| `tests/test_tushare_sync.py` | 通用同步器测试 | 新增守卫接入测试（重演 T12） |
| `tests/test_sync_daily_incremental.py` | 增量同步测试（新建） | 新建 + 守卫接入测试 |
| `tests/test_repair_gaps.py` | 补采测试 | 新增守卫接入测试 |
| `tests/test_dataset_registry.py` | 注册表测试 | 新增 daily 双轨收口断言 |
| `tests/data/test_freshness.py` | freshness 测试 | 新增行数骤降测试 |
| `.gitignore` | 忽略运行时状态 | 加 `data_lake/.freshness_baseline.json` |

---

## Task 1: 行数检查 SSoT 核心纯函数

**Files:**
- Modify: `data/integrity.py`（文件顶部 import 区 + 新增区块）
- Test: `tests/test_integrity.py`

**Interfaces:**
- Produces: `WRITE_GUARD_MIN_RATIO: float`、`WriteGuardError(RuntimeError)`、`existing_row_count(path: str) -> int | None`、`check_row_count_drop(baseline: int, new: int, min_ratio: float) -> tuple[bool, str]`。后续任务全部依赖这些签名。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_integrity.py`：

```python
import pandas as pd
import pytest

from data.integrity import (
    WRITE_GUARD_MIN_RATIO, WriteGuardError,
    existing_row_count, check_row_count_drop,
)


def test_write_guard_min_ratio_default_is_09():
    # 蓝图级默认阈值：新行数 < 现有 × 0.9 → 视为骤降拒写
    assert WRITE_GUARD_MIN_RATIO == 0.9


def test_check_row_count_drop_flags_crater():
    # T12 场景：1020万 → 3200，新行数远小于基线 × 0.9 → 判骤降
    ok, reason = check_row_count_drop(baseline=10_000_000, new=3200, min_ratio=0.9)
    assert ok is False
    assert "骤降" in reason or "drop" in reason.lower()


def test_check_row_count_drop_allows_growth():
    # 正常增量/增长：新行数 >= 基线 → 放行
    ok, _ = check_row_count_drop(baseline=1000, new=1005, min_ratio=0.9)
    assert ok is True


def test_check_row_count_drop_boundary_just_above_ratio():
    # 边界：新行数 = ceil(基线 × 0.9) → 刚好放行（>= ratio 不算骤降）
    ok, _ = check_row_count_drop(baseline=1000, new=900, min_ratio=0.9)
    assert ok is True


def test_check_row_count_drop_boundary_just_below_ratio():
    # 边界：新行数 = 基线 × 0.9 - 1 → 拒写
    ok, _ = check_row_count_drop(baseline=1000, new=899, min_ratio=0.9)
    assert ok is False


def test_existing_row_count_reads_metadata(tmp_path):
    # 物理意图：用 pyarrow 元数据读行数，免全量读 454MB parquet（freshness 注释 ~1.75s）
    p = tmp_path / "lake.parquet"
    pd.DataFrame({"a": range(1234)}).to_parquet(p)
    assert existing_row_count(str(p)) == 1234


def test_existing_row_count_none_when_missing(tmp_path):
    # 首次写/新湖：文件不存在 → None（无历史可比，放行）
    assert existing_row_count(str(tmp_path / "nope.parquet")) is None


def test_existing_row_count_none_on_corrupt(tmp_path):
    # 损坏文件 → None（调用方据此判「基线不可读」，由 assert_safe_overwrite 决策）
    p = tmp_path / "bad.parquet"
    p.write_bytes(b"not a parquet")
    assert existing_row_count(str(p)) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_integrity.py -k "write_guard_min_ratio or check_row_count_drop or existing_row_count" -v`
Expected: FAIL（`ImportError: cannot import name ...`，符号尚未定义）。

- [ ] **Step 3: 写最小实现**

在 `data/integrity.py` import 区加 `import pyarrow.parquet as pq`（若未引入），并在文件合适位置（`find_gaps` 之前，靠近其他规则常量）新增：

```python
# ============================================================================
# 写入前历史行数守卫（T13-A · L1 防抹除）
# ============================================================================
# 物理意图（T12 实证）：通用同步器 to_parquet 直接覆盖，无写入前守卫 →
# a_shares_daily 被 sync_tushare.py daily 残片覆盖（1020万→3200）。所有湖写入口
# （sync/repair/结果回湖）落盘前必须经本守卫：新行数相对现有骤降 → 拒写 + CRITICAL。
WRITE_GUARD_MIN_RATIO = 0.9   # 新行数 < 现有 × 0.9 → 视为骤降，拒写


class WriteGuardError(RuntimeError):
    """写入守卫拒绝：新行数相对现有骤降，疑为残片覆盖/部分回采，拒写保护历史。"""


def existing_row_count(path: str) -> int | None:
    """读 parquet 行数（pyarrow 元数据，不读数据体，免 454MB 全量 IO）。

    Returns:
        行数（文件存在且合法）；None（文件不存在或损坏——调用方据此区分「无基线」
        vs「基线不可读」）。
    """
    if not os.path.exists(path):
        return None
    try:
        return pq.read_metadata(path).num_rows
    except Exception:
        # 损坏/非 parquet：返 None，由 assert_safe_overwrite 决策（默认拒写，不静默）
        logger.warning("读 parquet 行数失败（损坏？）：%s", path, exc_info=True)
        return None


def check_row_count_drop(baseline: int, new: int,
                         min_ratio: float = WRITE_GUARD_MIN_RATIO) -> tuple[bool, str]:
    """行数骤降判定（SSoT 纯函数）：new < baseline × min_ratio → 骤降。

    freshness 行数骤降维度与写入守卫共用本函数（蓝图 §5 原则 2，禁止两套实现）。

    Args:
        baseline: 基线行数（写入守卫=现有文件行数；freshness=上次健康检查行数）。
        new:      待判定行数。
        min_ratio: 放行下限比（默认 0.9）。

    Returns:
        (ok, reason)：ok=True 放行；ok=False 骤降，reason 含中文结论供日志/断言。
    """
    if baseline <= 0:
        return True, "基线为 0/无历史，无骤降可言，放行"
    if new >= baseline * min_ratio:
        return True, f"new={new} >= baseline×{min_ratio}={int(baseline*min_ratio)}，放行"
    return False, (f"行数骤降：new={new} < baseline×{min_ratio}={int(baseline*min_ratio)}"
                   f"（baseline={baseline}），疑为残片覆盖/部分回采")
```

> 若 `data/integrity.py` 顶部尚未 `import os` / 定义 `logger`，先补 `import os` 与 `logger = logging.getLogger(__name__)`（跟随同文件既有风格）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_integrity.py -k "write_guard_min_ratio or check_row_count_drop or existing_row_count" -v`
Expected: PASS（8 条全绿）。

- [ ] **Step 5: 提交**

```bash
git add data/integrity.py tests/test_integrity.py
git commit -m "feat(data): 新增行数检查 SSoT 纯函数（check_row_count_drop/existing_row_count）

T13-A L1 写入守卫的判定内核 + freshness 行数骤降共用。
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 写入守卫编排 assert_safe_overwrite

**Files:**
- Modify: `data/integrity.py`（紧接 Task 1 区块）
- Test: `tests/test_integrity.py`

**Interfaces:**
- Consumes: Task 1 的 `existing_row_count`/`check_row_count_drop`/`WriteGuardError`/`WRITE_GUARD_MIN_RATIO`。
- Produces: `assert_safe_overwrite(lake_path: str, new_df: pd.DataFrame, *, min_ratio: float = 0.9, force: bool = False) -> None`。Task 3/4/5 全部调用本签名（落盘前置守卫）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_integrity.py`：

```python
from data.integrity import assert_safe_overwrite


def _write_lake(path, n):
    pd.DataFrame({"a": range(n)}).to_parquet(path)


def test_assert_safe_overwrite_raises_on_crater(tmp_path):
    # T12 核心断言：现有 10000 行，待写 100 行（骤降）→ 拒写抛异常
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 10000)
    tiny = pd.DataFrame({"a": range(100)})
    with pytest.raises(WriteGuardError):
        assert_safe_overwrite(str(p), tiny)
    # 拒写后原文件未被覆盖（行数不变）
    assert existing_row_count(str(p)) == 10000


def test_assert_safe_overwrite_passes_on_first_write(tmp_path):
    # 首次写/新湖：无现有文件 → 放行（无基线可比）
    p = tmp_path / "new.parquet"
    assert_safe_overwrite(str(p), pd.DataFrame({"a": range(50)}))  # 不抛


def test_assert_safe_overwrite_passes_on_growth(tmp_path):
    # 正常增量：现有 1000，待写 1200（增长）→ 放行
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 1000)
    assert_safe_overwrite(str(p), pd.DataFrame({"a": range(1200)}))


def test_assert_safe_overwrite_rejects_empty_new_df(tmp_path):
    # 空 df 落盘无意义且可能抹除 → 拒写
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 1000)
    with pytest.raises(WriteGuardError):
        assert_safe_overwrite(str(p), pd.DataFrame())


def test_assert_safe_overwrite_force_bypasses_but_logs(tmp_path, caplog):
    # 逃生口：force=True 旁路守卫（人为故意缩小重采），但仍 critical 留痕
    p = tmp_path / "lake.parquet"
    _write_lake(str(p), 10000)
    with caplog.at_level("CRITICAL", logger="data.integrity"):
        assert_safe_overwrite(str(p), pd.DataFrame({"a": range(100)}), force=True)
    assert any("FORCE" in r.message or "force" in r.message.lower() for r in caplog.records)


def test_assert_safe_overwrite_corrupt_existing_raises(tmp_path):
    # 现有文件损坏：基线不可读 → 拒写（宁拒不盲写），不静默放行
    p = tmp_path / "lake.parquet"
    p.write_bytes(b"not a parquet")
    with pytest.raises(WriteGuardError):
        assert_safe_overwrite(str(p), pd.DataFrame({"a": range(100)}))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_integrity.py -k assert_safe_overwrite -v`
Expected: FAIL（`ImportError: cannot import name 'assert_safe_overwrite'`）。

- [ ] **Step 3: 写最小实现**

在 `data/integrity.py` Task 1 区块末尾新增：

```python
def assert_safe_overwrite(lake_path: str, new_df: pd.DataFrame, *,
                          min_ratio: float = WRITE_GUARD_MIN_RATIO,
                          force: bool = False) -> None:
    """写入前历史行数守卫：落盘 to_parquet 前调用，骤降则抛 WriteGuardError 拒写。

    物理意图：封死 T12 式残片覆盖。所有湖写入口（通用同步器全量覆盖、增量 append、
    repair 重写、未来结果回湖）落盘前必须调本函数。

    决策矩阵（硬阻断语义，绝不静默）：
        force=True               → 旁路（人为故意缩小重采），CRITICAL 留痕后放行
        现有文件不存在            → 放行（无基线可比，首次写/新湖）
        现有文件损坏/不可读       → 拒写（基线不可读，宁拒不盲写）
        new_df 为空              → 拒写（空写无意义且可能抹除）
        new < baseline × min_ratio → 拒写（骤降，疑残片覆盖/部分回采）
        否则                     → 放行

    Args:
        lake_path: 落盘路径（读现有行数用）。
        new_df:    待写的 DataFrame（取 len 比对）。
        min_ratio: 骤降下限比（默认 0.9）。
        force:     逃生口（配合 QUANTER_FORCE_WRITE=1，调用方传入）。

    Raises:
        WriteGuardError: 拒写时抛，调用方应让其传播（阻断本次落盘）。
    """
    if force:
        # 逃生口留痕：守卫可拒绝、可强旁、不可静默
        logger.critical("FORCE 写入 %s（已旁路行数守卫，人为操作留痕）", lake_path)
        return
    existing = existing_row_count(lake_path)
    if existing is None and not os.path.exists(lake_path):
        return  # 首次写/新湖：无基线，放行
    new_len = len(new_df)
    if existing is None:
        # 文件存在但读不出（损坏）→ 拒写，不静默
        raise WriteGuardError(
            f"{lake_path} 现有文件损坏/行数不可读，拒写（基线不可信，宁拒不盲写）")
    if new_len == 0:
        raise WriteGuardError(f"{lake_path} 待写为空 df，拒写（空写无意义且可能抹除）")
    ok, reason = check_row_count_drop(existing, new_len, min_ratio)
    if not ok:
        logger.critical("写入守卫拒写 %s：%s", lake_path, reason)
        raise WriteGuardError(f"{lake_path} {reason}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_integrity.py -k assert_safe_overwrite -v`
Expected: PASS（6 条全绿）。

- [ ] **Step 5: 跑 integrity 全量回归**

Run: `python -m pytest tests/test_integrity.py -v`
Expected: PASS（既有 find_gaps/filter_universe 等测试不被破坏）。

- [ ] **Step 6: 提交**

```bash
git add data/integrity.py tests/test_integrity.py
git commit -m "feat(data): 新增 assert_safe_overwrite 写入前守卫（拒写+CRITICAL）

T13-A L1：所有湖写入口落盘前调用，骤降即拒写；QUANTER_FORCE_WRITE 旁路留痕。
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: 接入通用同步器全量覆盖写（T12 元凶路径）

> 这是最关键的接入点：`_sync_single` 与 `_build_multiindex` 都做**全量覆盖**（非 append），正是 T12 抹除路径。本任务重演并封死它。

**Files:**
- Modify: `data/tushare_sync.py:231`（`_build_multiindex` 末尾 `big.to_parquet`）、`:525`（`_sync_single` 末尾 `df.to_parquet`）
- Test: `tests/test_tushare_sync.py`（若不存在则 Create）

**Interfaces:**
- Consumes: Task 2 的 `assert_safe_overwrite`。
- Produces: 通用同步器两个落盘点受守卫保护。

- [ ] **Step 1: 写失败测试**

新建或追加 `tests/test_tushare_sync.py`：

```python
import pandas as pd
import pytest

from data.integrity import WriteGuardError


def _write_lake(path, n):
    pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n),
                  "symbol": ["000001.SZ"] * n,
                  "close": range(n)}).set_index(["date", "symbol"]).to_parquet(path)


def test_sync_single_refuses_crater_overwrite(tmp_path, monkeypatch):
    """重演 T12：现有大湖，_fetch_with_guard 回残片 → _sync_single 拒写，文件不变。"""
    from data import tushare_sync
    out = str(tmp_path / "lake.parquet")
    _write_lake(out, 10000)  # 现有 1 万行

    # mock 通用同步器的限频拉取，返回残片（模拟 SOCKS 异常只拿到单日）
    crater = pd.DataFrame({"trade_date": ["20260724"], "ts_code": ["000001.SZ"],
                           "close": [10.0]})
    monkeypatch.setattr(tushare_sync, "_fetch_with_guard",
                        lambda api, **kw: crater)
    # _sync_single 签名：(key, api, fields, date_col, out, cfg=None, start=None, end=None)
    with pytest.raises(WriteGuardError):
        tushare_sync._sync_single("fakekey", "daily", None, "trade_date", out,
                                  cfg={"api": "daily", "by": "single",
                                       "date_col": "trade_date",
                                       "symbol_col": "ts_code"})
    # 原湖未被覆盖
    from data.integrity import existing_row_count
    assert existing_row_count(out) == 10000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_tushare_sync.py::test_sync_single_refuses_crater_overwrite -v`
Expected: FAIL（守卫未接入，`_sync_single` 直接覆盖，`existing_row_count` 变成 1，断言失败；或 `WriteGuardError` 未抛）。

- [ ] **Step 3: 接入守卫**

在 `data/tushare_sync.py` 顶部 import 区加：

```python
from data.integrity import assert_safe_overwrite
```

`_sync_single`（:524-525）改为：

```python
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    assert_safe_overwrite(out, df,
                          force=os.environ.get("QUANTER_FORCE_WRITE") == "1")
    df.to_parquet(out, engine="pyarrow")
    logger.info("%s 写入：%s，%d 行", key, out, len(df))
```

`_build_multiindex`（:230-231）改为：

```python
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    assert_safe_overwrite(out, big,
                          force=os.environ.get("QUANTER_FORCE_WRITE") == "1")
    big.to_parquet(out, engine="pyarrow")
    logger.info("湖写入完成：%s，%d 行，%d 标的",
                out, len(big), big.index.get_level_values("symbol").nunique())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_tushare_sync.py::test_sync_single_refuses_crater_overwrite -v`
Expected: PASS。

- [ ] **Step 5: 跑 tushare_sync 全量回归**

Run: `python -m pytest tests/test_tushare_sync.py -v`
Expected: PASS（既有 _cleanse/_build_multiindex 正常路径测试不被破坏；首次写场景守卫放行）。

- [ ] **Step 6: 提交**

```bash
git add data/tushare_sync.py tests/test_tushare_sync.py
git commit -m "feat(data): 通用同步器全量覆盖写接入 assert_safe_overwrite

封死 T12 抹除路径（_sync_single/_build_multiindex 落盘前行数守卫）。
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: 接入增量同步与补采（append 路径，防御性）

> `sync_daily_incremental` 与 `repair_gaps` 都是 append/重写，combined 通常 >= 现有（守卫日常放行）；接入是为防御 dedup/recompute bug 导致的异常收缩。这两路若异常收缩，守卫同样拒写。

**Files:**
- Modify: `data/tools/sync_daily_incremental.py:244`、`data/tools/repair_gaps.py:172`
- Test: `tests/test_sync_daily_incremental.py`（Create）、`tests/test_repair_gaps.py`

**Interfaces:**
- Consumes: Task 2 的 `assert_safe_overwrite`。
- Produces: 增量同步与补采落盘受守卫保护。

- [ ] **Step 1: 写失败测试（repair_gaps）**

追加到 `tests/test_repair_gaps.py`：

```python
import pandas as pd
import pytest
from data.integrity import WriteGuardError, existing_row_count
from data.tools import repair_gaps as rg


def _lake(n):
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2024-01-01", periods=n), ["000001.SZ"]], names=["date", "symbol"])
    return pd.DataFrame({"close": range(n)}, index=idx)


def test_repair_gaps_main_refuses_shrink(tmp_path, monkeypatch):
    """repair_gaps 重写湖：若异常收缩（mock 出 bug），守卫拒写，文件不变。"""
    lake_path = tmp_path / "a_shares_daily.parquet"
    _lake(5000).to_parquet(lake_path)
    # 让 repair_gaps 内部产出的 new_lake 异常小（模拟 dedup/recompute bug）
    monkeypatch.setattr(rg, "repair_gaps", lambda gaps, lake_df, pro: _lake(100))
    monkeypatch.setattr(rg, "get_pro", lambda: object(), raising=False)
    monkeypatch.setattr(rg.pd, "read_parquet", lambda p: _lake(5000))
    with pytest.raises(WriteGuardError):
        rg.main(["--auto", "--lake-dir", str(tmp_path)])
    assert existing_row_count(str(lake_path)) == 5000
```

- [ ] **Step 2: 写失败测试（sync_daily_incremental）**

新建 `tests/test_sync_daily_incremental.py`：

```python
import pandas as pd
import pytest
from data.integrity import WriteGuardError, existing_row_count


def _lake(n_syms, n_days):
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2024-01-01", periods=n_days),
         [f"{i:06d}.SZ" for i in range(n_syms)]], names=["date", "symbol"])
    return pd.DataFrame({"close": range(len(idx))}, index=idx)


def test_sync_daily_refuses_shrink(tmp_path, monkeypatch):
    """增量同步：combined 异常收缩时守卫拒写。模拟 recompute/concat bug 致 shrink。"""
    from data.tools import sync_daily_incremental as sdi
    lake_path = tmp_path / "a_shares_daily.parquet"
    _lake(10, 500).to_parquet(lake_path)  # 现有 5000 行
    monkeypatch.setattr(sdi, "LAKE", str(lake_path))
    monkeypatch.setattr(sdi.pd, "read_parquet", lambda p: _lake(10, 500))
    # 让 combined 远小于现有（模拟异常）：把 to_parquet 前的 combined 替换成残片
    monkeypatch.setattr(sdi.pd.DataFrame, "to_parquet", lambda self, *a, **k: None)
    # 直接构造 shrink 场景：mock sync 主体使其产出 100 行 combined
    monkeypatch.setattr(sdi, "get_pro", lambda: object())
    # 用 force 路径反证守卫存在：默认应拒。这里改测守卫函数被调用——见下
    # 因 sync_daily_incremental 日常 append 必 >= 现有，构造真实 shrink 需深 mock；
    # 改为断言：守卫拒绝时 sync 不静默落盘（传播 WriteGuardError）。
    import data.integrity as itg
    monkeypatch.setattr(itg, "assert_safe_overwrite",
                        lambda *a, **k: (_ for _ in ()).throw(
                            WriteGuardError("stub shrink")))
    monkeypatch.setattr(sdi, "assert_safe_overwrite", itg.assert_safe_overwrite)
    with pytest.raises(WriteGuardError):
        sdi.sync_daily_incremental()
```

> 说明：增量路径日常 combined >= 现有（append），真实 shrink 极罕见；此处用 stub 守卫证明「守卫拒绝能传播、sync 不静默吞」。守卫本身的 shrink 判定已在 Task 2 单测覆盖。

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_repair_gaps.py::test_repair_gaps_main_refuses_shrink tests/test_sync_daily_incremental.py -v`
Expected: FAIL（守卫未接入，repair 不抛 / sync 不抛）。

- [ ] **Step 4: 接入守卫**

`data/tools/repair_gaps.py` 顶部 import 区加 `from data.integrity import assert_safe_overwrite, WriteGuardError`（`GapRange` 既有 import 旁）。`:172` 改为：

```python
    from data._tushare_compat import get_pro
    pro = get_pro()
    new_lake = repair_gaps(gaps, lake_df, pro)
    delta = len(new_lake) - len(lake_df)
    assert_safe_overwrite(str(lake_path), new_lake,
                          force=os.environ.get("QUANTER_FORCE_WRITE") == "1")
    new_lake.to_parquet(lake_path, engine="pyarrow")
    print(f"补采完成：a_shares_daily {len(lake_df)} → {len(new_lake)} 行（+{delta}）")
    return 0
```

`data/tools/sync_daily_incremental.py` 顶部 import 区加 `from data.integrity import assert_safe_overwrite`。`:244`（`combined.to_parquet(LAKE, ...)` 前）插入：

```python
    # 写入守卫（防御性：append 日常放行，捕获 dedup/recompute bug 致 combined 异常收缩）
    assert_safe_overwrite(LAKE, combined,
                          force=os.environ.get("QUANTER_FORCE_WRITE") == "1")
    combined.to_parquet(LAKE, engine="pyarrow")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_repair_gaps.py::test_repair_gaps_main_refuses_shrink tests/test_sync_daily_incremental.py -v`
Expected: PASS。

- [ ] **Step 6: 跑全量回归**

Run: `python -m pytest tests/test_repair_gaps.py tests/test_integrity.py -v`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add data/tools/repair_gaps.py data/tools/sync_daily_incremental.py tests/test_repair_gaps.py tests/test_sync_daily_incremental.py
git commit -m "feat(data): 增量同步/补采接入写入守卫（防御性 append 路径）

sync_daily_incremental + repair_gaps 落盘前 assert_safe_overwrite。
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: daily 双轨收口（registry）

> 删 `TUSHARE_DATASETS["daily"]`（让 `sync_tushare.py daily` 从 argparse `choices` 消失、全量重建路径不可达）+ 改 `DATASET_REGISTRY["daily"]["script"]` 指向增量脚本（让 sweep/POST /sync/daily 走增量）。`weekly`/`monthly` 保留不动。

**Files:**
- Modify: `config/registry.py:48-49`（DATASET_REGISTRY daily script）、`:707+`（删 TUSHARE_DATASETS["daily"] 整段）
- Test: `tests/test_dataset_registry.py`

**Interfaces:**
- Consumes: 无（配置收口）。
- Produces: daily 唯一写入口 = `sync_daily_incremental.py`；`sync_tushare.py daily` 不再可触发。

- [ ] **Step 1: 先核查无残留引用（避免删后断引用）**

Run: `grep -rn "TUSHARE_DATASETS\[.daily.\]\|TUSHARE_DATASETS.get(.daily\|sync_dataset(.daily" --include="*.py" .`
Expected: 仅命中 `config/registry.py` 定义处与 `data/tools/sync_tushare.py` 的 `choices=list(TUSHARE_DATASETS.keys())`（动态，安全）。**若命中其他显式 `["daily"]` 访问，先在对应处改线，再删 key。**

- [ ] **Step 2: 写失败测试**

追加到 `tests/test_dataset_registry.py`：

```python
def test_daily_removed_from_tushare_datasets():
    """daily 双轨收口：通用同步器不再认 daily（全量重建路径不可达）。"""
    from config import TUSHARE_DATASETS
    assert "daily" not in TUSHARE_DATASETS


def test_weekly_monthly_still_in_tushare_datasets():
    """周/月线仍走通用同步器（只收口 daily，不动周月）。"""
    from config import TUSHARE_DATASETS
    assert "weekly" in TUSHARE_DATASETS
    assert "monthly" in TUSHARE_DATASETS


def test_daily_registry_script_points_to_incremental():
    """sweep/POST /sync/daily 唯一入口 = 增量同步脚本。"""
    from config import DATASET_REGISTRY
    assert DATASET_REGISTRY["daily"]["script"] == "data/tools/sync_daily_incremental.py"


def test_sync_tushare_cli_no_longer_accepts_daily():
    """sync_tushare.py argparse choices 不再含 daily。"""
    from config import TUSHARE_DATASETS
    assert "daily" not in list(TUSHARE_DATASETS.keys())
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_dataset_registry.py -k "daily_removed or weekly_monthly or daily_registry_script or sync_tushare_cli_no_longer" -v`
Expected: FAIL（`"daily" in TUSHARE_DATASETS` 仍为 True；script 仍是 sync_tushare.py）。

- [ ] **Step 4: 改 registry**

`config/registry.py:48-49`（DATASET_REGISTRY daily 项）改为指向增量脚本 + 加收口注释：

```python
    # daily（前复权日线）：T13-A 双轨收口——唯一写入口 = sync_daily_incremental（增量
    # append + 除权重算 + backscan）。禁止再走 sync_tushare.py daily（通用同步器全量重建，
    # 无写入守卫，T12 实证致 1020万→3200 抹除）。TUSHARE_DATASETS 已删 "daily" key。
    "daily":         {"source": "Tushare", "market": "A股",  "granularity": "1d",
                      "script": "data/tools/sync_daily_incremental.py", "schedule": "每日18:00", "freshness_hours": 24},
```

删除 `config/registry.py:707` 起的 `TUSHARE_DATASETS["daily"]` 整段（从 `"daily": {` 到其对应 `},`，含上方「个股前复权日线」注释块）。在删除处留一行注释：

```python
    # 退役（T13-A · 2026-08-10）："daily" key 已删——daily 唯一写入口改走
    # sync_daily_incremental.py（增量），通用同步器不再认 daily（防 T12 式全量覆盖）。
    # 前复权日线历史全量（a_shares_daily.parquet）由增量脚本 backscan + repair_gaps 维护。
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_dataset_registry.py -v`
Expected: PASS（含新 4 条 + 既有注册表测试）。

- [ ] **Step 6: 跑全仓 import 冒烟（防删 key 致 import 期断引用）**

Run: `python -c "import config; from data.tushare_sync import sync_dataset; from data.tools.sync_tushare import main; print('import OK')"`
Expected: 打印 `import OK`（无异常）。

- [ ] **Step 7: 提交**

```bash
git add config/registry.py tests/test_dataset_registry.py
git commit -m "feat(config): daily 双轨收口——唯一写入口=sync_daily_incremental

删 TUSHARE_DATASETS['daily']（通用同步器不再认 daily）+ DATASET_REGISTRY daily
script 指向增量脚本。封死 T12 全量覆盖路径。周/月线不动。
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: freshness 行数骤降维度（sidecar 基线）

> freshness 当前只比 max-date（freshness.py:80），T12 单日湖 max-date=今天仍 PASS。本任务加行数骤降检测：每次检查读当前行数，与上次「健康行数」基线（sidecar）环比，骤降则 FAIL + CRITICAL。复用 Task 1 的 `check_row_count_drop`（SSoT）。

**Files:**
- Modify: `data/freshness.py`（`FreshnessResult` 加字段 + `check_freshness` 加骤降检测 + sidecar 读写）
- Modify: `.gitignore`（加 sidecar）
- Test: `tests/data/test_freshness.py`

**Interfaces:**
- Consumes: Task 1 的 `existing_row_count`/`check_row_count_drop`/`WRITE_GUARD_MIN_RATIO`。
- Produces: `FreshnessResult.row_count`；`check_freshness` 行数骤降 FAIL 语义。

- [ ] **Step 1: 写失败测试**

追加到 `tests/data/test_freshness.py`：

```python
import json
import pandas as pd
from data.freshness import check_freshness


def _write_daily_lake(lake_dir, n_rows):
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2026-08-01", periods=max(1, n_rows // 10)),
         [f"{i:06d}.SZ" for i in range(max(1, n_rows // 10))]],
        names=["date", "symbol"])[:n_rows]
    pd.DataFrame({"close": range(len(idx))}, index=idx).to_parquet(
        lake_dir / "a_shares_daily.parquet")


def test_freshness_records_row_count(tmp_path):
    _write_daily_lake(tmp_path, 500)
    r = check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))
    assert r.row_count == 500


def test_freshness_fails_on_row_count_crater(tmp_path):
    # 基线 10000 → 当前 100（骤降）：freshness 应 FAIL（即便 max-date 仍今天）
    _write_daily_lake(tmp_path, 10000)
    check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))  # 建基线
    _write_daily_lake(tmp_path, 100)  # 模拟残片覆盖
    r = check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))
    assert r.ok is False
    assert "骤降" in r.message or "行数" in r.message


def test_freshness_baseline_only_updates_on_healthy(tmp_path):
    # 基线只在健康（非骤降）时更新：骤降检查不应把基线拉低（防被掩盖）
    _write_daily_lake(tmp_path, 10000)
    check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))
    _write_daily_lake(tmp_path, 100)
    check_freshness("daily", "2026-08-01", lake_dir=str(tmp_path))  # 骤降，不更新基线
    baseline = json.loads((tmp_path / ".freshness_baseline.json").read_text())
    assert baseline["daily"]["row_count"] == 10000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/data/test_freshness.py -k "records_row_count or fails_on_row_count_crater or baseline_only_updates" -v`
Expected: FAIL（`FreshnessResult` 无 `row_count` 字段；无骤降检测）。

- [ ] **Step 3: 改 freshness.py**

`FreshnessResult` dataclass 加字段（`message` 之后）：

```python
    row_count: int | None = None    # 当前湖行数（骤降检测用；缺失/读失败则 None）
```

`check_freshness` 在读出 `latest` 后、返回 PASS/FAIL 前，插入骤降检测。把 `try` 块内改为同时取行数，并在函数末尾加 sidecar 环比。完整替换 `check_freshness` 函数体（保留 docstring）：

```python
def check_freshness(
    key: str,
    expected_date: str,
    *,
    lake_dir: str = "data_lake",
) -> FreshnessResult:
    """检查某数据集最新日期是否 >= 期望交易日，并检测行数骤降（T13-A）。

    行数骤降（T12 防线）：即便 latest_date >= expected_date，若当前行数相对上次
    健康基线骤降（< 基线 × WRITE_GUARD_MIN_RATIO），判 FAIL + CRITICAL——封死
    「max-date 是今天但历史被抹除」的盲区。基线存 sidecar，仅健康时更新（防被掩盖）。
    """
    fname = _KEY_TO_PARQUET.get(key, f"{key}.parquet")
    path = Path(lake_dir) / fname
    if not path.exists():
        msg = f"{key}({fname}) 缺失：{path} 不存在，期望 {expected_date} 数据未落湖"
        logger.warning(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg, row_count=None)

    try:
        import pandas as pd
        from data.integrity import (existing_row_count, check_row_count_drop,
                                    WRITE_GUARD_MIN_RATIO)
        df = pd.read_parquet(path)
        idx = df.index
        if isinstance(idx, pd.MultiIndex) and "date" in idx.names:
            dates = idx.get_level_values("date")
        else:
            dates = idx
        latest = str(pd.Timestamp(dates.max()).date())
        row_count = existing_row_count(str(path)) or len(df)
    except Exception as exc:
        msg = f"{key} 读最新日期/行数异常：{exc}（parquet 损坏？）"
        logger.exception(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg, row_count=None)

    # 行数骤降检测（sidecar 基线环比，复用 SSoT check_row_count_drop）
    crater_msg = _check_row_count_crater(key, row_count, lake_dir,
                                          WRITE_GUARD_MIN_RATIO)

    if latest < expected_date:
        msg = (f"{key} 数据陈旧：最新 {latest} < 期望 {expected_date}，"
               f"T 日数据未落湖（检查 Tushare 增量采集是否成功）")
        logger.warning(msg)
        return FreshnessResult(key, ok=False, latest_date=latest,
                               expected_date=expected_date, message=msg,
                               row_count=row_count)
    if crater_msg:
        # max-date 合格但行数骤降：T12 式抹除，FAIL + CRITICAL
        logger.critical("%s %s", key, crater_msg)
        return FreshnessResult(key, ok=False, latest_date=latest,
                               expected_date=expected_date,
                               message=f"{key} 最新 {latest} 合格，但{crater_msg}",
                               row_count=row_count)
    # 健康：更新基线（仅健康时写，防骤降被基线掩盖）
    _update_baseline(key, row_count, lake_dir)
    return FreshnessResult(key, ok=True, latest_date=latest,
                           expected_date=expected_date,
                           message=f"{key} 最新 {latest} >= 期望 {expected_date}，PASS",
                           row_count=row_count)
```

在 `check_freshness` 下方新增两个 sidecar 辅助函数：

```python
def _baseline_path(lake_dir: str) -> Path:
    return Path(lake_dir) / ".freshness_baseline.json"


def _check_row_count_crater(key: str, row_count: int, lake_dir: str,
                            min_ratio: float) -> str:
    """读 sidecar 基线，环比当前行数；骤降返中文结论，否则空串。

    物理意图：freshness 只看 max-date 会被「刚重写但内容是残片」骗过；本函数补行数维度。
    基线只在健康时更新（见 _update_baseline），骤降检查不拉低基线，防抹除被掩盖。
    无基线（首次）→ 不报骤降（返空），顺带由 _update_baseline 建基线。
    """
    import json
    bp = _baseline_path(lake_dir)
    if not bp.exists():
        return ""
    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("freshness 基线读失败（%s），跳过骤降检测", bp, exc_info=True)
        return ""
    baseline = data.get(key, {}).get("row_count")
    if not baseline:
        return ""
    ok, reason = check_row_count_drop(baseline, row_count, min_ratio)
    return "" if ok else reason


def _update_baseline(key: str, row_count: int, lake_dir: str) -> None:
    """健康检查后更新 sidecar 基线（仅健康调用方调用，故此处无条件写当前行数）。"""
    import json
    bp = _baseline_path(lake_dir)
    try:
        data = json.loads(bp.read_text(encoding="utf-8")) if bp.exists() else {}
    except Exception:
        data = {}
    data[key] = {"row_count": row_count}
    bp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
```

- [ ] **Step 4: 加 .gitignore**

在 `.gitignore` 末尾加：

```
# T13-A freshness 行数骤降 sidecar 基线（运行时状态，不入库）
data_lake/.freshness_baseline.json
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/data/test_freshness.py -v`
Expected: PASS（新 3 条 + 既有 freshness 测试；既有测试若断言 `FreshnessResult(...)` 位置参数需确认兼容——加了带默认值的 `row_count` 不破坏位置参数）。

> 若既有测试用 `FreshnessResult(key, ok=..., ...)` 关键字或 4 位置参数构造，加默认值字段向后兼容；若用 `assert r == FreshnessResult(...)` 全字段比较，需同步更新那些断言（加 `row_count`）。跑全量 freshness 测试即可暴露。

- [ ] **Step 6: 跑 freshness 调用方回归**

Run: `python -m pytest tests/data/test_run_data_check_data_ready.py tests/broadcast/test_brief_data_freshness.py -v`
Expected: PASS（check_freshness 调用方不受破坏）。

- [ ] **Step 7: 提交**

```bash
git add data/freshness.py .gitignore tests/data/test_freshness.py
git commit -m "feat(data): freshness 加行数骤降维度（sidecar 基线环比）

封死 T12「max-date 合格但历史被抹除」盲区；复用 check_row_count_drop SSoT。
基线仅健康时更新，防骤降被掩盖。
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: 全链路回归 + 文档回填

> 收尾：一个重演 T12 全场景的集成测试（守卫拒写 + freshness 检测），并把 Wave A 完成状态回填到债务文档与 T13 工单。

**Files:**
- Test: `tests/test_t13a_write_guard_e2e.py`（Create）
- Modify: `docs/architecture/06-tech-debt.md`（Wave A 完成标注）、`plans/wayfinder/T13-blueprint.md`（§0 状态）、`plans/wayfinder/T13.md`（Resolution 占位）

**Interfaces:**
- Consumes: Task 1-6 全部产出。

- [ ] **Step 1: 写 e2e 测试**

新建 `tests/test_t13a_write_guard_e2e.py`：

```python
"""T13-A 全链路：重演 T12 抹除场景，验证守卫拒写 + freshness 检测双保险。"""
import pandas as pd
import pytest
from data.integrity import WriteGuardError, existing_row_count
from data.freshness import check_freshness


def _big_lake(path, n=10000):
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=100), [f"{i:06d}.SZ" for i in range(n // 100)]],
        names=["date", "symbol"])
    pd.DataFrame({"close": range(len(idx))}, index=idx).to_parquet(path)


def test_t12_scenario_blocked_and_detected(tmp_path, monkeypatch):
    lake = tmp_path / "a_shares_daily.parquet"
    _big_lake(str(lake))

    # 第一道保险：freshness 先建健康基线
    check_freshness("daily", "2026-04-09", lake_dir=str(tmp_path))

    # 第二道保险：通用同步器试图残片覆盖 → 守卫拒写
    from data import tushare_sync
    crater = pd.DataFrame({"trade_date": ["20260409"], "ts_code": ["000001.SZ"], "close": [1.0]})
    monkeypatch.setattr(tushare_sync, "_fetch_with_guard", lambda api, **kw: crater)
    with pytest.raises(WriteGuardError):
        tushare_sync._sync_single("daily", "daily", None, "trade_date", str(lake),
                                  cfg={"api": "daily", "by": "single",
                                       "date_col": "trade_date", "symbol_col": "ts_code"})
    assert existing_row_count(str(lake)) == 10000  # 原湖完好

    # 第三道保险：即便守卫被旁路，freshness 下次检查也应检测到骤降
    _big_lake(str(lake), n=100)  # 强行模拟覆盖后的小湖
    r = check_freshness("daily", "2026-04-09", lake_dir=str(tmp_path))
    assert r.ok is False  # 行数骤降被检出
```

- [ ] **Step 2: 跑 e2e 确认通过**

Run: `python -m pytest tests/test_t13a_write_guard_e2e.py -v`
Expected: PASS（三道保险全部生效）。

- [ ] **Step 3: 跑全仓回归**

Run: `python -m pytest tests/test_integrity.py tests/test_tushare_sync.py tests/test_repair_gaps.py tests/test_sync_daily_incremental.py tests/test_dataset_registry.py tests/data/test_freshness.py tests/test_t13a_write_guard_e2e.py -v`
Expected: PASS。

- [ ] **Step 4: 文档回填**

`docs/architecture/06-tech-debt.md` 债务热力图：把 **data 完整性** 项的 L1 子项标注「T13-A 写入守卫完成（2026-08-XX）」（保留 L2/L3 仍为 crit，因 Wave B/C 未做）。在 Critical 表「data 完整性 gate 缺陷」行物理事实补一句：`L1 写入守卫 + daily 双轨收口 + freshness 行数骤降已治理（T13-A）；L2 scan gate + L3 自动补采仍欠（T13-B）`。

`plans/wayfinder/T13-blueprint.md` §0：把 Wave A 标 ✅。

`plans/wayfinder/T13.md`：在 `## Resolution` 区（若无则新增）留 `T13-A（L1 写入侧安全网）完成于 2026-08-XX，见 docs/superpowers/plans/2026-08-10-t13-a-write-side-safety-net.md；T13-B/C 待续。`

> 文档里的日期用实际完成日（UTC 当天）。Wave B/C 状态保持「待续」。

- [ ] **Step 5: 提交**

```bash
git add tests/test_t13a_write_guard_e2e.py docs/architecture/06-tech-debt.md plans/wayfinder/T13-blueprint.md plans/wayfinder/T13.md
git commit -m "test+docs(t13a): 全链路 T12 回归测试 + Wave A 完成回填

三道保险（freshness 基线 / 写入守卫拒写 / freshness 骤降检测）e2e 验证。
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review（计划自检，写完后跑）

1. **Spec coverage**：蓝图 Wave A = #6 写入守卫 + #4 双轨收口 + freshness 行数骤降 → Task 1-2（守卫内核）、Task 3-4（四个写入口接入）、Task 5（双轨收口）、Task 6（freshness 骤降）、Task 7（回归）。✅ 全覆盖，无遗漏。
2. **Placeholder scan**：无 TBD/TODO；所有测试含可执行代码；所有实现含完整函数体。✅
3. **Type consistency**：`assert_safe_overwrite(lake_path, new_df, *, min_ratio=0.9, force=False)` 在 Task 2 定义，Task 3/4 全部一致调用；`check_row_count_drop(baseline, new, min_ratio) -> (ok, reason)` Task 1 定义、Task 6 复用一致；`FreshnessResult.row_count` Task 6 定义。✅
4. **行数 SSoT 一致性**：`check_row_count_drop` 仅 Task 1 定义，Task 6 freshness 复用（不重写判定逻辑）。✅
5. **逃生口一致**：四个写入口均用 `os.environ.get("QUANTER_FORCE_WRITE") == "1"`，命名统一。✅
6. **不破坏周月**：Task 5 仅删 `daily`，测试显式断言 weekly/monthly 仍在。✅
