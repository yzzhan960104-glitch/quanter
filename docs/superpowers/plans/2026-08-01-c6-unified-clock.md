# C-6 时间统一上下文 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** trading 包内所有 `datetime.now()` 收口到 `trading/clock.py` 单一时间源（now/today/trading_day 三函数），触发点入口缓存防同轮跨午夜漂移，测试 monkeypatch 单一口子。

**Architecture:** 模块级 `trading/clock.py`（扁平，不封 Clock 类）提供 `now()`/`today()`/`trading_day()`（today=pre_open 读口径，trading_day=eod 落盘口径=next_trading_day(today)，命名区分读/写避免 eod/pre_open 混淆）。engine.py 18 处 + position_book 4 处 + order_state 2 处 + pipeline 1 处 datetime.now 按用途收口（key→today/timestamp→now/eod 落盘→trading_day）；触发点入口缓存 _today/_td 防漂移。

**Tech Stack:** Python 3.10、APScheduler、pytest（`asyncio.run` 范式 + monkeypatch）。

## Global Constraints

- **全中文注释**（CLAUDE.md，What + Why 交易物理意图）。
- **clock.py 三函数（verbatim 签名）**：`now() -> datetime`、`today() -> str`（YYYY-MM-DD）、`trading_day() -> str`（= `calendar.next_trading_day(today())`）。
- **不封 Clock 类**（模块级函数扁平，Karpathy 极简）。
- **不凝固时间**（clock 无状态，每次调 datetime.now()；防漂移靠触发点入口缓存，不靠 clock 内部缓存）。
- **today/trading_day 命名区分（红线）**：eod 必用 `trading_day()`（落盘 key），pre_open/_stoploss/_post_open 必用 `today()`（读 key）；禁止混用。
- **不动 C-4/C-5 gate 决议**（_critical_guard/_health_guard/_gw_health_gate 语义不变；clock 只替换时间源）。
- **仅 trading 包收口**（presentation/broadcast/discovery/broker 不改）。
- **测试 monkeypatch `trading.clock`**（单一口子，替代 patch 各模块 datetime）。
- **全量回归基线**：C-5 后 **1158 passed / 0 failed**，本期零退化。
- **测试入口**：`F:/quanter/.venv310/Scripts/python.exe -m pytest ...`（下文简写 `pytest`）。
- **commit 规范**：`feat(c6-vN): ...` / `fix(c6-vN): ...`，结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。

## File Structure

| 文件 | 责任 | 本期改动 |
|---|---|---|
| `trading/clock.py` | 单一时间源口子（新建） | **V1**：新建，三函数 now/today/trading_day。 |
| `tests/trading/test_clock.py` | clock 单测（新建） | **V1**：三函数单测。 |
| `trading/engine.py` | TradingEngine 四触发点 + 模块级 eod_plan/pre_open | **V2**：18 处 datetime.now → clock（key→today/timestamp→now/eod→trading_day）+ 触发点入口缓存 + pre_open 内部 today_eq/today_for_max_wait 改用传入 date 参数。 |
| `trading/position_book.py` | 持仓账本 | **V3**：line 161 today → clock.today；line 160/230/265 now → clock.now。 |
| `trading/order_state.py` | 订单状态 | **V3**：line 59 order_id + line 189 time → clock.now。 |
| `trading/orchestrate/pipeline.py` | pipeline_then_eod 编排 | **V3**：line 52 today → clock.today。 |
| `tests/trading/test_e2e_trading_flow.py` | e2e 跨日时序 | **V4**：line 77（patch position_book.datetime）+ line 850（patch pipeline.datetime）→ patch `trading.clock`（单一口子）+ 新 e2e clock freeze。 |

---

## Task 1 (V1)：clock.py + 单测

**Files:**
- Create: `trading/clock.py`
- Test: `tests/trading/test_clock.py`

**Interfaces:**
- Consumes: `trading.calendar.next_trading_day(date_str)`（既有，clock.trading_day 复用）。
- Produces: `trading.clock.now() -> datetime` / `today() -> str` / `trading_day() -> str`（V2/V3 下游消费）。

- [ ] **Step 1：写失败测试（新建 test_clock.py）**

创建 `tests/trading/test_clock.py`：

```python
# -*- coding: utf-8 -*-
"""C-6 V1：trading/clock.py 单一时间源单测。

物理意图（spec §3.1 · [[eod-date-offbyone-fix]] 教训）：
    now/today/trading_day 三函数是 trading 包时间统一口子。today=pre_open 读口径，
    trading_day=eod 落盘口径（next_trading_day(today)），命名区分读/写避免 eod/pre_open 混淆。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import trading.clock as clock
from trading.calendar import next_trading_day


def test_now_returns_datetime():
    """now() 返 datetime（事件时间戳用）。"""
    result = clock.now()
    assert isinstance(result, datetime)


def test_today_format():
    """today() 返 YYYY-MM-DD 字符串（pre_open 读 plan key 口径）。"""
    fixed = datetime(2026, 7, 28, 15, 30, 0)
    with patch("trading.clock.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        assert clock.today() == "2026-07-28"


def test_trading_day_equals_next_trading_day_of_today():
    """trading_day() == next_trading_day(today())（eod 落盘 key 口径）。

    物理意图：eod（T 日盘后）落 plan_T+1，pre_open（T+1 开盘前）读 plan_T+1。
    trading_day 命名区分读/写口径——避免 eod/pre_open key 错位。
    """
    fixed = datetime(2026, 7, 28, 15, 30, 0)  # 周二
    with patch("trading.clock.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        # next_trading_day("2026-07-28") 真实算（依赖 calendar，周二→周三 2026-07-29）
        assert clock.trading_day() == next_trading_day("2026-07-28")


def test_trading_day_neq_today():
    """trading_day() != today()（key 错位防线——eod 落盘日 ≠ 今日）。"""
    fixed = datetime(2026, 7, 28, 15, 30, 0)
    with patch("trading.clock.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        assert clock.trading_day() != clock.today()
```

- [ ] **Step 2：跑测试验证失败**

Run: `pytest tests/trading/test_clock.py -v`
Expected: 4 用例全 FAIL（`ModuleNotFoundError: No module named 'trading.clock'`）。

- [ ] **Step 3：实现 clock.py（完整代码）**

创建 `trading/clock.py`：

```python
# -*- coding: utf-8 -*-
"""C-6 单一时间源口子。trading 包内所有 datetime.now() 替换为本模块函数。

物理意图（[[eod-date-offbyone-fix]] 教训）：
    时间源散落（engine.py 18 处 + position_book/order_state/pipeline）→ 同轮跨午夜
    漂移 + 测试冻结需 patch 多处 datetime + 未来 eod/pre_open 类似 key 漂移无防线。
    本模块提供单一口子：测试 monkeypatch trading.clock 即冻结全包时间。

三函数命名区分读/写口径（避免 eod/pre_open 混淆，spec §3.1）：
    - today()       = 今日（pre_open 读 plan key 口径）
    - trading_day() = 次交易日（eod 落盘 plan key 口径 = next_trading_day(today)）
    - now()         = 当前 datetime（事件时间戳 submitted_at/order_id/written_at）

不凝固时间（clock 无状态，每次调 datetime.now()）：防同轮跨午夜漂移靠触发点入口缓存
（engine._eod/_pre_open/_stoploss/_post_close 入口算一次 _today/_td 传下游），不靠
clock 内部缓存——进程级缓存会让长跑服务时间凝固不真实。
"""
from __future__ import annotations

from datetime import datetime

from trading.calendar import next_trading_day


def now() -> datetime:
    """当前 datetime（单一时间源口子，事件时间戳用）。

    测试 monkeypatch trading.clock.now 冻结全包时间（替代 patch 各模块 datetime）。
    """
    return datetime.now()


def today() -> str:
    """今日 YYYY-MM-DD（pre_open 读 plan key 口径）。

    用途：load_plan/save_plan 读 key、is_trading_day 守卫、holding_days 计算。
    禁止 eod 落盘用本函数（eod 必用 trading_day，避免 key 错位）。
    """
    return now().strftime("%Y-%m-%d")


def trading_day() -> str:
    """次交易日（eod 落盘 plan key 口径 = next_trading_day(today)）。

    物理意图：eod（T 日盘后）落 plan_T+1，pre_open（T+1 开盘前）读 plan_T+1。
    today() 与 trading_day() 命名区分读/写口径——避免 eod/pre_open key 错位
    （[[eod-date-offbyone-fix]] 病灶：原 eod 用 today 落盘，pre_open 读 today 永远差一天）。
    """
    return next_trading_day(today())
```

- [ ] **Step 4：跑测试验证通过**

Run: `pytest tests/trading/test_clock.py -v`
Expected: 4 用例全 PASS。

- [ ] **Step 5：commit**

```bash
git add trading/clock.py tests/trading/test_clock.py
git commit -m "feat(c6-v1): trading/clock.py 单一时间源（now/today/trading_day）

- now() 事件时间戳；today() pre_open 读口径；trading_day() eod 落盘口径(=next_trading_day(today))
- 命名区分读/写避免 eod/pre_open 混淆（[[eod-date-offbyone-fix]] 教训）
- 模块级扁平不封 Clock 类；无状态不凝固（防漂移靠入口缓存 V2）
- test_clock.py 4 用例（now/today 格式/trading_day=next_trading_day(today)/trading_day≠today）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2 (V2)：engine.py 收口（18 处 datetime.now → clock + 入口缓存）

**Files:**
- Modify: `trading/engine.py`（18 处 datetime.now + 触发点入口缓存 + pre_open 内部 today 用 date 参数）
- 回归: `tests/trading/test_engine*.py`（既有用例应绿——clock.today==today 字面，但若 patch datetime 需 V4 迁移）

**Interfaces:**
- Consumes: `trading.clock.now/today/trading_day`（V1 产出）。
- Produces: engine.py 内所有时间操作走 clock（单一口子）；触发点入口缓存 _today/_td。

**替换规则（按用途分三类，implementer 按上下文判断每处）：**
| 用途 | 替换为 | 识别特征 |
|---|---|---|
| 业务日期 key（读/写 plan、is_trading_day、holding_days、熔断基线 date） | `clock.today()` 或用入口缓存的 `date` 参数 | `strftime("%Y-%m-%d")` 后用于 load_plan/save_plan/is_trading_day/snapshot_start_equity |
| eod 落盘 key | `clock.trading_day()` | _eod 入口算次日交易日传 eod_plan(date) |
| 事件时间戳（submitted_at、written_at、is_intraday_session 时点） | `clock.now()` | `.isoformat()` 或传 is_intraday_session(datetime) |

- [ ] **Step 1：grep 确认 engine.py 全部 datetime.now 命中点**

Run: `grep -nE "datetime\.now\(\)" F:/quanter/trading/engine.py`
Expected: 18 行命中（line 736/779/862/956/970/1300/1502/1546/1623/1644/1663/1701/2173/2228/2428/2478/2589/2684/2816 等，行号随编辑漂移，以 grep 实时为准）。逐行按替换规则分类（key/timestamp/eod）。

- [ ] **Step 2：_eod 入口收口（eod 落盘 key 用 clock.trading_day）**

定位 `_eod` 方法入口（engine.py:2228 附近，`async def _eod(self) -> None:` 的 docstring 后）：

old_string：
```python
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("eod_plan 跳过：今日非交易日 %s", today)
            return
```

new_string：
```python
        # C-6 V2：单一时间源 + 入口缓存（防同轮跨午夜漂移）。
        # _today=交易日守卫口径（clock.today），_td=eod 落盘 key 口径（clock.trading_day，
        # =next_trading_day(today)）。命名区分读/写避免 eod/pre_open key 错位。
        _today = clock.today()
        if not calendar.is_trading_day(_today):
            logger.info("eod_plan 跳过：今日非交易日 %s", _today)
            return
        _td = clock.trading_day()
```

**然后**：`_eod` 内部所有用 `today`（原 datetime.now 变量）传 eod_plan/scan 的地方，改用 `_td`（落盘 key）或 `_today`（交易日/T 日 scan 截止）。grep `_eod` 函数体内 `today` 用法，按语义替换：
- 传 `eod_plan(date=...)` 的 date → `_td`（落盘 T+1）
- `_load_df_upto(lake, sym, today)` 的 scan 截止日 → `_today`（T 日盘后扫到 T）
- 实际 `_eod` 内部 `today` 变量名 → 重命名为 `_today`/`_td` 按语义（避免与原 `today` 混淆）

implementer 用 grep `_eod` 函数体（`async def _eod` 到下一个 `async def`/`def` 前）的 `today` 出现点，逐个判断。

- [ ] **Step 3：pre_open(date) 内部 today 用传入 date 参数（入口缓存传递）**

`pre_open(date)` 已有 `date` 参数（外部 `_pre_open` 传 today）。内部 3 处 datetime.now 改用 `date` 或 clock.now：

**3a. line 736 today_eq（熔断基线 key）**——用传入 date：
old_string：
```python
    today_eq = datetime.now().strftime("%Y-%m-%d")
```
new_string：
```python
    # C-6 V2：用传入 date（入口缓存，_pre_open 已算 clock.today 传 pre_open），不重复 datetime.now。
    today_eq = date
```

**3b. line 779 today_for_max_wait（max_wait 窗口 key）**——用传入 date：
old_string：
```python
    today_for_max_wait = datetime.now().strftime("%Y-%m-%d")
```
new_string：
```python
    # C-6 V2：用传入 date（入口缓存，防同轮跨午夜漂移）。
    today_for_max_wait = date
```

**3c. line 862 submitted_at（事件时间戳）**——用 clock.now：
old_string：
```python
                    submitted_at=datetime.now().isoformat())
```
new_string：
```python
                    submitted_at=clock.now().isoformat())
```

- [ ] **Step 4：_stoploss / _post_close 入口 today 用 clock.today**

C-5 V4 已在 _stoploss/_post_close 入口加 `_gw_health_gate`。gate 之后、交易日守卫处的 `today = datetime.now()...` 改 clock.today。

`_stoploss` 入口（docstring 后、gate 后的交易日守卫）：
old_string（C-5 V4 后的 _stoploss today 行，参考 engine.py:2478 附近）：
```python
        today = datetime.now().strftime("%Y-%m-%d")
        # 交易日守卫（Task 8 fix · review I1）：IntervalTrigger 无 1-5 工作日过滤，
```
new_string：
```python
        # C-6 V2：单一时间源 + 入口缓存（clock.today，防同轮跨午夜漂移）。
        today = clock.today()
        # 交易日守卫（Task 8 fix · review I1）：IntervalTrigger 无 1-5 工作日过滤，
```

`_post_close` 入口同款（engine.py:2589 附近的 `today = datetime.now()...` → `today = clock.today()`）。

- [ ] **Step 5：其余 datetime.now 按规则收口**

grep 剩余 datetime.now（line 956/970/1300/1502/1546/1623/1644/1663/1701/2173/2684/2816 等），按替换规则分类收口。代表：

- **line 956 `is_intraday_session(datetime.now())`**（stop_loss_monitor 时段判定，时点非 key）→ `is_intraday_session(clock.now())`
- **line 970/1502/1546/1623/1644/1663/1701 today_eq/_today**（post_close 等业务日期 key）→ `clock.today()`
- **line 1300/2684 written_at**（事件时间戳）→ `clock.now().isoformat()`
- **line 2173 _today**（_verify_eod_calendar_alignment 启动口径自检）→ `clock.today()`，自检内 `calendar.next_trading_day(_today)` 逻辑不变（验 clock.trading_day() != clock.today()）

implementer 逐行看上下文（key→clock.today / timestamp→clock.now）。

- [ ] **Step 6：顶部 import clock**

engine.py 顶部 import 区加：
```python
from trading import clock
```
（或 `from trading.clock import now as _clock_now, today as _clock_today, trading_day as _clock_trading_day`——选与 engine 既有风格一致的；engine 既有 `from trading import state_store as _state_store` 风格，故用 `from trading import clock` + `clock.today()` 调用）。

- [ ] **Step 7：跑 engine 全套测试**

Run: `pytest tests/trading/test_engine.py tests/trading/test_engine_pre_open_gate.py tests/trading/test_engine_stoploss_inject.py tests/trading/test_stoploss_post_close_gate.py tests/trading/test_engine_bootstrap.py tests/trading/test_e2e_trading_flow.py -v`
Expected: 大部分 PASS（clock.today==today 字面，行为不变）。**若 test_e2e_trading_flow.py:77/850 patch position_book/pipeline.datetime 失效**（因 V3 才改 position_book/pipeline，此时仍用 datetime），暂保留失败 → V4 迁移修复（V2 step 不强求 test_e2e 绿，V3 改 position_book/pipeline 后 V4 统一迁移）。

若其它 engine 测试因 patch `trading.engine.datetime` 失效（如 test_engine.py 某 fixture patch datetime），改 patch `trading.clock`（同款迁移，V2 内联处理）。

- [ ] **Step 8：commit**

```bash
git add trading/engine.py
git commit -m "feat(c6-v2): engine.py 18 处 datetime.now 收口到 clock + 入口缓存

- _eod 入口 _today=clock.today()/_td=clock.trading_day()（落盘 key 用 trading_day）
- pre_open 内部 today_eq/today_for_max_wait 改用传入 date（入口缓存传递）
- submitted_at/written_at 等时间戳用 clock.now()
- _stoploss/_post_close 入口 today 用 clock.today()
- _verify_eod_calendar_alignment 启动自检用 clock.today
- 单一时间源口子 + 入口缓存防同轮跨午夜漂移

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3 (V3)：position_book + order_state + pipeline 收口

**Files:**
- Modify: `trading/position_book.py`（4 处：line 161 today → clock.today；160/230/265 now → clock.now）
- Modify: `trading/order_state.py`（2 处：line 59/189 → clock.now）
- Modify: `trading/orchestrate/pipeline.py`（line 52 today → clock.today）

**Interfaces:**
- Consumes: `trading.clock.now/today`（V1 产出）。
- Produces: position_book/order_state/pipeline 时间操作走 clock。

- [ ] **Step 1：position_book 收口（4 处）**

grep 确认：`grep -nE "datetime\.now\(\)" F:/quanter/trading/position_book.py` → 4 处（line 160/161/230/265）。

- **line 161 `today = datetime.now().strftime("%Y-%m-%d")`**（业务日期 key）→ `today = clock.today()`
- **line 160/230/265 `now = datetime.now().isoformat()`**（事件时间戳）→ `now = clock.now().isoformat()`

顶部加 `from trading import clock`（或 `from trading.clock import now as _clock_now, today as _clock_today`，与 position_book 既有风格一致）。

- [ ] **Step 2：order_state 收口（2 处）**

grep 确认：`grep -nE "datetime\.now\(\)" F:/quanter/trading/order_state.py` → 2 处。

- **line 59 `self.order_id = f"ORDER_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"`**（唯一性 order_id）→ `self.order_id = f"ORDER_{clock.now().strftime('%Y%m%d%H%M%S%f')}"`
- **line 189 `"time": datetime.now()`**（事件时间戳）→ `"time": clock.now()`

顶部加 `from trading import clock`。

- [ ] **Step 3：pipeline 收口（1 处）**

`trading/orchestrate/pipeline.py:52`：
old_string：
```python
    today = datetime.now().strftime("%Y-%m-%d")
    if not is_trading_day(today):
```
new_string：
```python
    # C-6 V3：单一时间源（pipeline_then_eod 入口 today 用 clock.today）。
    today = clock.today()
    if not is_trading_day(today):
```

顶部加 `from trading import clock`（pipeline.py 既有 import 风格）。

- [ ] **Step 4：跑相关测试**

Run: `pytest tests/trading/test_position_book.py tests/trading/test_order_state.py tests/trading/test_pipeline.py tests/trading/test_engine.py -v 2>&1 | tail -20`
Expected: 大部分 PASS。若 test_e2e_trading_flow.py:77/850 patch position_book/pipeline.datetime 失效（因 position_book/pipeline 现用 clock，patch datetime 不再生效）→ V4 迁移修复。其余测试 PASS。

- [ ] **Step 5：commit**

```bash
git add trading/position_book.py trading/order_state.py trading/orchestrate/pipeline.py
git commit -m "feat(c6-v3): position_book/order_state/pipeline 收口到 clock

- position_book: today(161)→clock.today; now(160/230/265)→clock.now
- order_state: order_id(59)+time(189)→clock.now
- pipeline: today(52)→clock.today
- trading 包 datetime.now 全收口（grep 仅命中 clock.py 内部）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4 (V4)：测试 patch 迁移 + e2e clock freeze + 全量回归

**Files:**
- Modify: `tests/trading/test_e2e_trading_flow.py:77,850`（patch position_book/pipeline.datetime → patch trading.clock）
- Create: `tests/trading/test_clock_e2e_freeze.py`（e2e clock freeze：eod/pre_open key 对齐）
- 回归: 全量 tests/

**Interfaces:**
- Consumes: `trading.clock`（V1-V3 产出）。
- Produces: 测试 patch 单一口子（trading.clock）+ e2e key 对齐回归。

- [ ] **Step 1：迁移 test_e2e_trading_flow.py patch（R2）**

**1a. line 77 patch position_book.datetime**——C-6 后 position_book 用 clock，patch datetime 不再生效。改 patch trading.clock：

old_string（line 71-77 附近）：
```python
    # 冻结 position_book 内 datetime 至 T+1 日（让 applied_at 与 date 前缀匹配）
    from datetime import datetime as _RealDT
    class _FrozenDT(_RealDT):
        @classmethod
        def now(cls, tz=None):
            return _RealDT(2026, 7, 28, 15, 30, 0)
    monkeypatch.setattr(position_book, "datetime", _FrozenDT)
```

new_string：
```python
    # C-6 V4：position_book 已收口 clock（V3），patch 单一口子 trading.clock.now 冻结时间。
    from datetime import datetime as _RealDT
    _frozen = _RealDT(2026, 7, 28, 15, 30, 0)
    monkeypatch.setattr("trading.clock.now", lambda: _frozen)
```

**1b. line 850 patch pipeline_mod.datetime**——同款迁移：

old_string（line 850 附近）：
```python
    monkeypatch.setattr(pipeline_mod, "datetime", _FrozenDT)
```

new_string：
```python
    # C-6 V4：pipeline 已收口 clock（V3），patch trading.clock.now（单一口子）。
    monkeypatch.setattr("trading.clock.now", lambda: _frozen)
```

（`_frozen` 定义同 1a，或测试内复用；若 line 850 在另一函数，该函数内补 `_frozen` 定义。）

**1c. 若其它测试 patch `trading.engine.datetime` / position_book.datetime**（V2/V3 改 clock 后失效）——grep 定位：
Run: `grep -rnE "patch.*datetime|setattr.*datetime" F:/quanter/tests/trading/ --include="*.py"`
对每处确认是否 patch 了已收口模块（engine/position_book/order_state/pipeline）的 datetime，是则迁移到 patch trading.clock.now/today。

- [ ] **Step 2：跑 test_e2e_trading_flow.py 确认 patch 迁移生效**

Run: `pytest tests/trading/test_e2e_trading_flow.py -v`
Expected: 全 PASS（patch trading.clock.now 让 eod/pre_open/position_book/pipeline 时间冻结一致，跨日时序可复现）。

- [ ] **Step 3：新建 e2e clock freeze 测试（回归 [[eod-date-offbyone-fix]]）**

创建 `tests/trading/test_clock_e2e_freeze.py`：

```python
# -*- coding: utf-8 -*-
"""C-6 V4：e2e clock freeze——eod 落盘 key 与 pre_open 读 key 对齐（单一口子冻结）。

物理意图（spec §4 · [[eod-date-offbyone-fix]] 回归）：
    monkeypatch trading.clock.now 返固定时间（T 日盘后），eod 落 plan_T+1，
    pre_open（仍冻结同一时间或 T+1）读 plan_T+1，key 对齐。
    C-6 前：patch 多处 datetime（position_book/pipeline/engine）才能冻结；
    C-6 后：patch trading.clock.now 单一口子即冻结全包。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import trading.clock as clock


def test_clock_freeze_single_source():
    """patch trading.clock.now 单一口子 → today/trading_day 一致派生。"""
    fixed = datetime(2026, 7, 28, 19, 0, 0)  # 周二盘后
    with patch("trading.clock.now", lambda: fixed):
        assert clock.today() == "2026-07-28"
        assert clock.trading_day() != "2026-07-28"  # 次交易日（周三）
        # 同轮多次调用结果一致（无跨午夜漂移）
        assert clock.today() == clock.today()


def test_eod_pre_open_key_alignment_under_frozen_clock():
    """eod 落盘 key（trading_day）= pre_open 读 key（today）当 pre_open 在 T+1。

    构造：冻结 T 日盘后 → eod trading_day() = T+1 → 冻结 T+1 盘前 → pre_open today() = T+1。
    两 key 相等 = 对齐（[[eod-date-offbyone-fix]] 病灶回归）。
    """
    # T 日盘后（周二 2026-07-28 19:00）
    t_eod = datetime(2026, 7, 28, 19, 0, 0)
    with patch("trading.clock.now", lambda: t_eod):
        eod_key = clock.trading_day()  # eod 落盘 key
    # T+1 盘前（周三 2026-07-29 09:22）
    t_pre_open = datetime(2026, 7, 29, 9, 22, 0)
    with patch("trading.clock.now", lambda: t_pre_open):
        pre_open_key = clock.today()  # pre_open 读 key
    assert eod_key == pre_open_key, (
        f"eod 落盘 key ({eod_key}) 必须 = pre_open 读 key ({pre_open_key})，"
        "否则 confirmed 计划存在但 pre_open 全部 reason=无计划（[[eod-date-offbyone-fix]] 病灶）")
```

- [ ] **Step 4：跑 clock e2e freeze 测试**

Run: `pytest tests/trading/test_clock_e2e_freeze.py -v`
Expected: 2 用例 PASS。

- [ ] **Step 5：全量回归（spec §6 验收 6 · C-5 后 1158 基线零退化）**

Run: `cd F:/quanter && .venv310/Scripts/python.exe -m pytest tests/ -q`
Expected: **1158 passed / 0 failed** + 本期新增（V1 test_clock 4 + V4 test_clock_e2e_freeze 2 = +6）→ 约 **1164 passed / 0 failed**。允许数差异（V2-V4 改造可能调整既有用例 patch），但 **0 failed** 是硬指标。

若 failed：按失败信息回 V1-V3 修复。

- [ ] **Step 6：验收 grep（spec §6 验收 2 · trading 包 datetime.now 仅命中 clock.py）**

Run: `grep -rnE "datetime\.now\(\)" F:/quanter/trading/ --include="*.py"`
Expected: 仅命中 `trading/clock.py` 内部（`return datetime.now()`）。engine/position_book/order_state/pipeline 0 命中（全收口）。

- [ ] **Step 7：spec §6 验收 1-7 逐条核对 + commit**

逐条核对 spec §6：
1. clock.py 三函数 ✓（V1）
2. trading 包 datetime.now 收口（grep 仅 clock.py）✓（Step 6）
3. 触发点入口缓存 ✓（V2）
4. eod=clock.trading_day / pre_open=clock.today 对齐 ✓（V4 e2e freeze）
5. 测试 patch 迁移 + test_clock + e2e freeze ✓（V4）
6. 全量 1158+ 基线零退化 ✓（Step 5）
7. C-4/C-5 gate 不变 ✓（V2 不动 gate）

```bash
git add tests/trading/test_e2e_trading_flow.py tests/trading/test_clock_e2e_freeze.py
git commit -m "test(c6-v4): 测试 patch 迁移 trading.clock + e2e clock freeze + 全量回归

- test_e2e_trading_flow.py:77,850 patch position_book/pipeline.datetime → patch trading.clock（单一口子）
- 新建 test_clock_e2e_freeze.py（eod/pre_open key 对齐回归 [[eod-date-offbyone-fix]]）
- 全量 1164 passed / 0 failed（C-5 基线 1158 + V1/V4 新增 6）
- spec §6 验收 1-7 全绿；grep datetime.now 仅命中 clock.py

Co-Authored-By: Claude <noreply@anthropic.com>"
```

- [ ] **Step 8：更新 memory（controller 执行，非 implementer）**

C-6 全部 Task 完成后，controller 写 `~/.claude/projects/F--quanter/memory/c6-unified-clock-status.md` + `MEMORY.md` 索引（implementer 不动 memory）。

---

## Self-Review

**1. Spec 覆盖**：
- spec §3.1 clock.py 三函数 → Task 1 ✓
- spec §3.2 替换规则三类 → Task 2/3（engine 18 处 + position_book/order_state/pipeline）✓
- spec §3.3 触发点入口缓存 → Task 2 Step 2/3/4（_eod/_pre_open/_stoploss/_post_close）✓
- spec §4 测试（test_clock + e2e freeze + R2 patch 迁移）→ Task 1 + Task 4 ✓
- spec §6 验收 1-7 → Task 4 Step 6/7 逐条 ✓

**2. Placeholder 扫描**：无 TBD/TODO；每个 step 含完整代码或精确 grep+规则；Task 2 Step 5「其余 datetime.now 按规则」给规则 + 代表行（非 placeholder，implementer 按规则 + grep 清单逐行判断）。

**3. 类型一致性**：`clock.now()->datetime` / `today()->str` / `trading_day()->str` 在 Task 1 定义、Task 2/3/4 消费，签名一致 ✓；`today/trading_day` 命名区分贯穿（eod=trading_day，pre_open/_stoploss/_post_close=today）✓。

**4. 连带处理**：
- test_e2e_trading_flow.py:77,850 patch 迁移 → Task 4 Step 1 ✓
- 其它测试 patch datetime → Task 4 Step 1c grep 定位 ✓
- pre_open 内部 today_eq/today_for_max_wait 用 date 参数（入口缓存传递）→ Task 2 Step 3 ✓
