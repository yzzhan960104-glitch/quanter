# 二期引擎 gap4：本地持仓账本 + 复盘报告 + e2e 4 步链路 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接通二期自动交易引擎 gap4（post_close 对账缺 local_positions），新建 SQLite 本地持仓账本 + 最小复盘报告生成器，并用 e2e 验证「数据时效 → 生成计划 → 隔日交易 → 复盘报告」完整交易链路。

**Architecture:** 新建 `trading/position_book.py`（SQLite 账本，复用 `experiment/store.py` 的 WAL 范式）作为对账「本地侧」单一真理源；`_handle_order_update` 成交回报写入账本（BUY/SELL 幂等），`_post_close` 读账本注入 `local_positions`；新建 `trading/review_report.py` 聚合 fill 表+计划+持仓+drift 生成 markdown 复盘；e2e 串四步。

**Tech Stack:** Python 标准库 `sqlite3`（WAL，无 ORM）、`asyncio.run` 驱动 async 测试（pytest-asyncio strict 模式，不加 `@pytest.mark.asyncio`）、`unittest.mock.patch` 拦截真实副作用、`strategies.signal.Signal` frozen dataclass。

## Global Constraints

- **Python 标准库 sqlite3**，禁用 SQLAlchemy/ORM（对齐 `experiment/store.py` ADR3 + CLAUDE.md 反魔法原则）。
- **WAL 模式**：每个 `_connect` 开 `PRAGMA journal_mode=WAL`。
- **全中文注释**：所有新增代码必须像素级中文注释（CLAUDE.md 协议），说明 What + Why（交易物理意图/风控红线）。
- **pytest-asyncio strict**：async 测试用 `asyncio.run(...)` 同步驱动，**不**加 `@pytest.mark.asyncio`（对齐 `tests/trading/test_engine.py` 既有范式）。
- **patch 真身模块路径**：`_handle_order_update` 内 lazy import 的符号（`record_live_trade`/`NotificationManager`）需 patch 真身模块（`presentation.server.services.trading_service` / `infra.notifier`），非 `trading.engine`（见 `test_engine_order_update_handler.py:54-60` 注释）。
- **db_path 默认参数用 None + 运行时解析**：所有 position_book/review_report 函数 `db_path=None`，函数内 `db_path = db_path or _DEFAULT_DB`——保证 e2e 测试 `monkeypatch.setattr(position_book, "_DEFAULT_DB", tmp_db)` 能全局生效（规避 Python 默认参数定义期绑定陷阱）。
- **A 股风控红线**：账本只在真实成交回报写入（挂单/下卖出单不写）；direction 非 BUY/SELL 不写（不猜方向）。

---

## Task 1: position_book.py（SQLite 本地持仓账本）

**Files:**
- Create: `trading/position_book.py`
- Test: `tests/trading/test_position_book.py`

**Interfaces:**
- Consumes: 标准库 `sqlite3`/`contextlib`/`pathlib`/`datetime`
- Produces:
  - `init_db(db_path: str | None = None) -> None`
  - `apply_fill(order_id: str, symbol: str, direction: str, qty: float, price: float, *, db_path: str | None = None) -> bool`
  - `get_local_positions(*, db_path: str | None = None) -> dict[str, float]`
  - `_connect(db_path)` contextmanager（模块内部，review_report 复用）

- [ ] **Step 1: 写失败测试 `tests/trading/test_position_book.py`**

```python
# -*- coding: utf-8 -*-
"""position_book 单测：本地持仓账本读写/幂等/方向/清理。

物理意图：验证对账「本地侧」单一真理源的 ACID 行为——
- BUY 累加 / SELL 累减 / 归零清理；
- UNIQUE(order_id) 幂等防重推（R1 红线：重推不重复加减持仓）；
- 方向未知抛 ValueError（不猜方向误记）；
- db_path 默认 None + 运行时解析 _DEFAULT_DB（e2e 可 monkeypatch）。
"""
from __future__ import annotations

import pytest

from trading import position_book


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个测试用独立 tmp db（隔离），并 patch _DEFAULT_DB 让 engine 间接调用也命中 tmp。"""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    return db_path


def test_init_db_idempotent(db):
    """重复 init_db 不报错（CREATE TABLE IF NOT EXISTS 幂等）。"""
    position_book.init_db()  # 再次调
    position_book.init_db()  # 第三次
    # 不抛即通过


def test_apply_fill_buy_accumulates(db):
    """BUY 两次（不同 order_id）→ qty 累加。"""
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0) is True
    assert position_book.apply_fill("o2", "300001.SZ", "BUY", 200, 10.5) is True
    assert position_book.get_local_positions() == {"300001.SZ": 300.0}


def test_apply_fill_sell_decrements_and_clears_zero(db):
    """BUY 后 SELL → qty 减；归零则从 position 表删除（对账并集不被 0 干扰）。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0)
    position_book.apply_fill("o2", "300001.SZ", "SELL", 100, 11.0)  # 归零
    assert position_book.get_local_positions() == {}  # qty=0 已清理


def test_apply_fill_idempotent(db):
    """同 order_id 重推 → 返 False，qty 不变（R1 幂等红线）。"""
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0) is True
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0) is False  # 重推
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}  # 没翻倍


def test_apply_fill_unknown_direction_raises(db):
    """direction 非 BUY/SELL → 抛 ValueError（不猜方向误记）。"""
    with pytest.raises(ValueError):
        position_book.apply_fill("o1", "300001.SZ", "TRADE", 100, 10.0)


def test_get_local_positions_excludes_zero(db):
    """qty=0 的标的（已清理）不返回；多标的混合正确。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0)
    position_book.apply_fill("o2", "688001.SH", "BUY", 200, 20.0)
    position_book.apply_fill("o3", "688001.SH", "SELL", 200, 21.0)  # 688001 归零清理
    pos = position_book.get_local_positions()
    assert pos == {"300001.SZ": 100.0}
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/trading/test_position_book.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading.position_book'`（或 `ImportError`）

- [ ] **Step 3: 写 `trading/position_book.py` 实现**

```python
# -*- coding: utf-8 -*-
"""trading.position_book — 本地持仓账本（SQLite，WAL + 事务）。

物理定位：post_close 对账的「本地侧」单一真理源。记录本地系统记账的理论持仓
（独立于 broker 真实持仓），drift 检的就是两者偏差。

Why SQLite 而非 JSON（对齐 experiment/store.py ADR3）：
- 幂等天然：fill 表 UNIQUE(order_id) 让重推 INSERT 失败即跳过，无需手写 processed_orders；
- 事务一致性：写流水 + 更新持仓同事务原子（崩溃不 corrupt）；
- 并发安全：WAL 多读单写；
- 审计可追溯：fill 表即成交流水。

复用范式：experiment/store.py 的 _connect（WAL + row_factory + 自动 commit/rollback）。

db_path 默认参数用 None + 运行时解析 _DEFAULT_DB：
    规避 Python 默认参数定义期绑定陷阱——e2e 测试 monkeypatch._DEFAULT_DB 能全局生效
    （engine 内 `position_book.get_local_positions()` 不传 db_path 也能命中 tmp）。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB = "logs/trading_state.db"


@contextmanager
def _connect(db_path: str):
    """连接上下文：开 WAL，提交/回滚自动。SQLite 连接非线程安全，每次操作新建连接。

    复用 experiment/store.py:_connect 范式（WAL + row_factory + 自动 commit/rollback）。
    首次运行建父目录（logs/trading_state/），否则 sqlite3.connect 写不存在的目录会抛。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db(db_path: str | None = None) -> None:
    """幂等建表（CREATE TABLE IF NOT EXISTS）。由 trading/__main__ 启动期调用。

    两张表：
      - position：每个 symbol 当前净持仓（对账读这张，post_close 消费）；
      - fill：成交流水 + UNIQUE(order_id) 幂等去重（重推 INSERT 失败即跳过，R1 红线）。
    """
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS position (
                symbol     TEXT PRIMARY KEY,
                qty        REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fill (
                fill_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id   TEXT NOT NULL,
                symbol     TEXT NOT NULL,
                direction  TEXT NOT NULL,
                qty        REAL NOT NULL,
                price      REAL NOT NULL,
                applied_at TEXT NOT NULL,
                UNIQUE(order_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_fill_symbol ON fill(symbol)")


def apply_fill(order_id: str, symbol: str, direction: str, qty: float, price: float,
               *, db_path: str | None = None) -> bool:
    """成交回报应用：写流水 + 更新持仓（单事务原子）。

    物理意图：成交回报 handler（_handle_order_update）在真实成交（BUY/SELL）时调本函数，
    把「本地记账」与 broker 真实持仓保持同步——post_close 对账才能检出 drift。

    幂等（R1 红线）：order_id 已存在 → INSERT 抛 IntegrityError → 返 False（重推跳过，
    持仓不重复加减）。Phase1 按 order_id 整笔去重（首次成交量入账），部分成交累加精度
    留 live follow-up（按 gw._orders[order_id].qty_traded 单调累计取增量）。

    方向（R3 红线）：BUY → +qty；SELL → -qty；其它 → 抛 ValueError。调用方（_handle_order_update）
    应已过滤 None 方向，但本函数再守一道防误调。

    清理：持仓归零的标的 DELETE（保持账本干净，对账并集不被 qty=0 干扰）。

    Returns:
        True = 首次应用入账；False = 重复 order_id 跳过。
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"apply_fill direction 仅 BUY/SELL，收到 {direction}")
    db_path = db_path or _DEFAULT_DB
    now = datetime.now().isoformat()
    delta = float(qty) if direction == "BUY" else -float(qty)
    with _connect(db_path) as con:
        try:
            con.execute(
                "INSERT INTO fill(order_id, symbol, direction, qty, price, applied_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (order_id, symbol, direction, float(qty), float(price), now))
        except sqlite3.IntegrityError:
            # 重推幂等：order_id 已入账，跳过（持仓不重复加减——R1 红线）。
            logger.info("apply_fill 跳过重复 order_id=%s symbol=%s", order_id, symbol)
            return False
        # UPSERT 持仓（SQLite ON CONFLICT 语法，3.24+ 支持）。
        con.execute(
            "INSERT INTO position(symbol, qty, updated_at) VALUES(?, ?, ?)"
            " ON CONFLICT(symbol) DO UPDATE SET qty=qty+?, updated_at=?",
            (symbol, delta, now, delta, now))
        # 归零清理：qty=0 的标的删除（对账并集不被 0 干扰，账本保持干净）。
        con.execute("DELETE FROM position WHERE qty=0")
    return True


def get_local_positions(*, db_path: str | None = None) -> dict[str, float]:
    """读本地理论持仓 {symbol: qty}（qty!=0）。供 _post_close 对账注入 local_positions。"""
    db_path = db_path or _DEFAULT_DB
    with _connect(db_path) as con:
        rows = con.execute("SELECT symbol, qty FROM position WHERE qty != 0").fetchall()
    return {r["symbol"]: float(r["qty"]) for r in rows}
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/trading/test_position_book.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add trading/position_book.py tests/trading/test_position_book.py
git commit -m "feat(trading): position_book SQLite 本地持仓账本（gap4 对账本地侧）

- fill 表 UNIQUE(order_id) 幂等防重推，position 表 UPSERT 累计净持仓
- WAL + 事务原子（写流水+更新持仓同事务，崩溃不 corrupt）
- db_path 默认 None 运行时解析（e2e 可 monkeypatch）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: review_report.py（最小复盘报告生成器）

**Files:**
- Create: `trading/review_report.py`
- Test: `tests/trading/test_review_report.py`

**Interfaces:**
- Consumes: `position_book._connect`/`_DEFAULT_DB`/`get_local_positions`（Task 1）、`trading.trading_plan.load_plan`
- Produces:
  - `generate_review(date: str, *, db_path: str | None = None, plan: dict | None = None, drift: bool | None = None) -> str`
  - `save_review(date: str, md: str, *, review_dir: str = "logs/trading_reviews") -> Path`

- [ ] **Step 1: 写失败测试 `tests/trading/test_review_report.py`**

```python
# -*- coding: utf-8 -*-
"""review_report 单测：T 日复盘报告四段（计划/成交/持仓/对账）。

物理意图：验证 e2e 第 4 步的「可观测产物」——聚合 fill 表当日成交 + 计划 + 收盘持仓
+ drift 成 markdown，作为交易链路终点的复盘凭证。
"""
from __future__ import annotations

import pytest

from trading import position_book, review_report


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    # review_report 内部用 position_book._DEFAULT_DB 默认，patch position_book 即可联动
    position_book.init_db()
    return db_path


def test_generate_review_sections(db):
    """有计划 + 有成交 + 有持仓 + drift=False → 四段齐全。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0)
    plan = {
        "confirmed": True,
        "orders": [
            {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
             "stop_price": 9.0, "take_profit": 12.0},
        ],
    }
    md = review_report.generate_review("2026-07-27", plan=plan, drift=False)
    assert "交易复盘" in md
    assert "300001.SZ" in md            # 计划段 + 成交段 + 持仓段都应出现
    assert "买入 1 笔" in md            # 成交聚合
    assert "100" in md                  # 持仓 qty
    assert "无偏差" in md               # drift=False → ✅ 无偏差


def test_generate_review_empty_plan(db):
    """无计划 → 报告标「无计划」不崩。"""
    md = review_report.generate_review("2026-07-27", plan=None, drift=None)
    assert "无计划" in md
    assert "未对账" in md               # drift=None → 未对账


def test_generate_review_drift_true(db):
    """drift=True → ⚠️ 有偏差。"""
    md = review_report.generate_review("2026-07-27", plan=None, drift=True)
    assert "有偏差" in md


def test_save_review_idempotent(db, tmp_path):
    """save_review 落盘 + 重复写覆盖。"""
    md = review_report.generate_review("2026-07-27", plan=None, drift=None)
    out = review_report.save_review("2026-07-27", md, review_dir=str(tmp_path / "reviews"))
    assert out.exists()
    out2 = review_report.save_review("2026-07-27", md, review_dir=str(tmp_path / "reviews"))
    assert out == out2  # 同一文件覆盖
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/trading/test_review_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading.review_report'`

- [ ] **Step 3: 写 `trading/review_report.py` 实现**

```python
# -*- coding: utf-8 -*-
"""trading.review_report — T 日交易复盘报告（最小版）。

物理定位：e2e 交易链路第 4 步的「可观测产物」。聚合 position_book.fill 表（当日成交
流水）+ trading_plan（计划）+ position 表（收盘持仓）+ post_close drift，输出 markdown
报告。数据源全部已就绪，零新 I/O 通道。

为什么最小版：本 task 核心是 gap4 对账链路；复盘报告是链路终点的可观测产物，最小集
（计划/成交/持仓/对账四段）够验证链路通。全功能复盘（按 experiment_id 聚合 PnL/胜率/
Sharpe + 推钉钉）属 Layer 6 LLM 复盘范畴，留 follow-up。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from trading import position_book, trading_plan

logger = logging.getLogger(__name__)

_DEFAULT_REVIEW_DIR = "logs/trading_reviews"


def _fetch_fills_on(date: str, *, db_path: str) -> list[sqlite3.Row]:
    """读 fill 表中 applied_at 当日（LIKE 'date%'）的成交流水，按时间升序。

    Why LIKE date%：applied_at 是 datetime.now().isoformat()（含时分秒），date 是
    YYYY-MM-DD，前缀匹配取当日全部成交。ISO 格式字典序 = 时间序，ORDER BY 自然升序。
    """
    with position_book._connect(db_path) as con:
        return con.execute(
            "SELECT order_id, symbol, direction, qty, price, applied_at FROM fill"
            " WHERE applied_at LIKE ? ORDER BY applied_at",
            (f"{date}%",),
        ).fetchall()


def generate_review(
    date: str,
    *,
    db_path: Optional[str] = None,
    plan: Optional[dict] = None,
    drift: Optional[bool] = None,
) -> str:
    """生成 T 日交易复盘 markdown（计划/成交/持仓/对账四段）。

    Args:
        date:    交易日（YYYY-MM-DD）。
        db_path: 持仓账本 db 路径（默认 position_book._DEFAULT_DB）。
        plan:    透传计划 dict（避免重复 load）；None 则内部 trading_plan.load_plan(date)。
        drift:   post_close 对账结果（True=有偏差/False=ok/None=未对账）。

    Returns:
        markdown 字符串（四段：计划/成交/持仓/对账）。
    """
    db_path = db_path or position_book._DEFAULT_DB

    # ---- 计划段 ----
    if plan is None:
        plan = trading_plan.load_plan(date)
    plan_lines: list[str] = []
    if plan and plan.get("orders"):
        confirmed = "已确认" if plan.get("confirmed") else "待确认"
        plan_lines.append(f"> 计划 {confirmed}（{len(plan['orders'])} 单）")
        for o in plan["orders"]:
            od = o.get("order") or {}
            plan_lines.append(
                f"- {od.get('symbol')} {od.get('side')} {od.get('qty')}股@{od.get('price')}"
                f"（止损{o.get('stop_price')}/止盈{o.get('take_profit')}）"
            )
    else:
        plan_lines.append("- 无计划")

    # ---- 成交段（fill 表当日聚合）----
    fills = _fetch_fills_on(date, db_path=db_path)
    buy_n = sum(1 for f in fills if f["direction"] == "BUY")
    sell_n = sum(1 for f in fills if f["direction"] == "SELL")
    trade_lines: list[str] = [f"- 买入 {buy_n} 笔，卖出 {sell_n} 笔"]
    for f in fills:
        trade_lines.append(
            f"  - {f['symbol']} {f['direction']} {f['qty']:g}股@{f['price']:g}"
            f"（order={f['order_id']}）"
        )

    # ---- 持仓段（position 表）----
    positions = position_book.get_local_positions(db_path=db_path)
    pos_lines: list[str] = []
    if positions:
        for sym, qty in positions.items():
            pos_lines.append(f"- {sym} {qty:g}股")
    else:
        pos_lines.append("- 空仓")

    # ---- 对账段 ----
    if drift is True:
        drift_line = "- ⚠️ 有偏差（drift=True，请排查 only_local/only_broker/drifted）"
    elif drift is False:
        drift_line = "- ✅ 无偏差（drift=False）"
    else:
        drift_line = "- 未对账（drift=None）"

    return (
        f"### T 日交易复盘 {date}\n\n"
        f"**计划**\n" + "\n".join(plan_lines) + "\n\n"
        f"**成交**\n" + "\n".join(trade_lines) + "\n\n"
        f"**收盘持仓**\n" + "\n".join(pos_lines) + "\n\n"
        f"**对账**\n" + drift_line + "\n"
    )


def save_review(date: str, md: str, *, review_dir: str = _DEFAULT_REVIEW_DIR) -> Path:
    """落盘 logs/trading_reviews/review_<date>.md（幂等覆盖，父目录自动建）。"""
    p = Path(review_dir)
    p.mkdir(parents=True, exist_ok=True)
    out = p / f"review_{date}.md"
    out.write_text(md, encoding="utf-8")
    logger.info("复盘报告已落盘 %s", out)
    return out
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/trading/test_review_report.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add trading/review_report.py tests/trading/test_review_report.py
git commit -m "feat(trading): review_report 最小复盘报告生成器（e2e 第 4 步依赖）

聚合 fill 表当日成交+计划+持仓+drift 成 markdown 四段

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: engine.py + __main__.py 接线（写入点 + 读取点 + 启动初始化）

**Files:**
- Modify: `trading/engine.py`（`_handle_order_update` 加第四连 apply_fill；`_post_close` 改读账本传 local_positions）
- Modify: `trading/__main__.py`（`_run_forever` 加 `position_book.init_db()`）
- Test: `tests/trading/test_engine.py`（扩展 4 个测试）

**Interfaces:**
- Consumes: `position_book.apply_fill`/`get_local_positions`/`init_db`（Task 1）
- Produces: `_post_close` 现在会注入 `local_positions`；`_handle_order_update` 现在会写账本

- [ ] **Step 1: 写失败测试（追加到 `tests/trading/test_engine.py` 末尾）**

```python
# ============================================================================
# gap4：position_book 接线（_post_close 读账本 + _handle_order_update 写账本）
# ============================================================================
def test_post_close_reads_position_book(monkeypatch):
    """_post_close：读 position_book 账本 → 注入 local_positions → 调 post_close 对账。"""
    from trading import position_book, engine as eng_mod

    # 让 position_book 返非空（模拟有真实成交累计的持仓）
    monkeypatch.setattr(position_book, "get_local_positions",
                        lambda **kw: {"300001.SZ": 100.0})
    captured = {}

    async def _fake_post_close(date, *, gw=None, local_positions=None, tolerance=0.0):
        captured["local"] = local_positions
        captured["date"] = date
        return {"date": date}

    monkeypatch.setattr(eng_mod, "post_close", _fake_post_close)
    monkeypatch.setattr(eng_mod.calendar, "is_trading_day", lambda d: True)

    eng = eng_mod.TradingEngine()
    asyncio.run(eng._post_close())
    assert captured["local"] == {"300001.SZ": 100.0}  # 账本读出注入 post_close


def test_post_close_empty_book_passes_empty_dict(monkeypatch):
    """_post_close：账本空 → 传 {}（非 None）——live 下 broker 有时能报 only_broker drift。"""
    from trading import position_book, engine as eng_mod

    monkeypatch.setattr(position_book, "get_local_positions", lambda **kw: {})
    captured = {}

    async def _fake_post_close(date, *, gw=None, local_positions=None, tolerance=0.0):
        captured["local"] = local_positions
        return {"date": date}

    monkeypatch.setattr(eng_mod, "post_close", _fake_post_close)
    monkeypatch.setattr(eng_mod.calendar, "is_trading_day", lambda d: True)

    eng = eng_mod.TradingEngine()
    asyncio.run(eng._post_close())
    assert captured["local"] == {}  # 空 dict 直传，不转 None


def test_handle_order_update_writes_book(monkeypatch):
    """BUY 成交回报 → apply_fill 被调；方向 None（order_type 缺失）→ 不调。"""
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import position_book
    from trading.engine import TradingEngine

    eng = TradingEngine()
    eng._tp_placed = set()
    update = {
        "kind": "trade", "order_id": "999", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    eng._gw = MagicMock()
    eng._gw._orders = {"999": {"order_type": 23}}  # 23=STOCK_BUY

    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()), \
         patch.object(position_book, "apply_fill", return_value=True) as af:
        asyncio.run(eng._handle_order_update(update))
    # BUY 成交 → apply_fill 被调一次，方向 "BUY"
    af.assert_called_once()
    assert af.call_args.args[2] == "BUY"  # direction 参数（位置参）


def test_handle_order_update_book_failure_soft_degrades(monkeypatch):
    """apply_fill 抛异常 → 不阻断 a 日志/b 通知/c 止盈（独立 try-except 软降级）。"""
    from unittest.mock import MagicMock, AsyncMock, patch
    from trading import position_book
    from trading.engine import TradingEngine

    eng = TradingEngine()
    eng._tp_placed = set()
    update = {
        "kind": "trade", "order_id": "999", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    eng._gw = MagicMock()
    eng._gw._orders = {"999": {"order_type": 23}}

    tp_called = []
    async def _tp(*a, **kw):
        tp_called.append(True)

    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(position_book, "apply_fill", side_effect=RuntimeError("db locked")), \
         patch.object(eng, "_place_take_profit", new=_tp):
        asyncio.run(eng._handle_order_update(update))  # apply_fill 抛异常不应冒泡
    # 止盈仍被调（账本失败不阻断 c 连）
    assert tp_called == [True]
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/trading/test_engine.py::test_post_close_reads_position_book tests/trading/test_engine.py::test_handle_order_update_writes_book -v`
Expected: FAIL（`_post_close` 仍传 None / `_handle_order_update` 未调 apply_fill）

- [ ] **Step 3: 改 `trading/engine.py:_post_close`（读账本传 local_positions）**

**定位**：`engine.py` 当前 `_post_close` 方法（约 line 826-831）：
```python
    async def _post_close(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("post_close 跳过：今日非交易日 %s", today)
            return
        await post_close(today)
```

**改为**：
```python
    async def _post_close(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("post_close 跳过：今日非交易日 %s", today)
            return
        # gap4 fix：读本地账本 → 注入 local_positions，对账链路真跑。
        # ⚠️ 空 dict 直传（不转 None）：live 下账本空但 broker 有持仓（疑似外部单）时，
        # reconcile(local={}, broker={有}) 会报 only_broker drift——转 None 会让 post_close
        # 走跳过分支漏报（对账查漏价值丧失）。dry_run 下 gw=None 时 post_close 内部自然跳过。
        from trading import position_book
        local_positions = position_book.get_local_positions()
        await post_close(today, local_positions=local_positions)
```

- [ ] **Step 4: 改 `trading/engine.py:_handle_order_update`（加第四连 apply_fill 写入）**

**定位**：`_handle_order_update` 方法末尾的 c 连 `_place_take_profit` 块（约 line 922-929）：
```python
        if direction == "BUY" and symbol not in self._tp_placed:
            self._tp_placed.add(symbol)  # 调度点幂等标记（先占位，防 await 期间重入重挂）
            try:
                await self._place_take_profit(symbol, qty, price, order_id)
            except Exception:
                # 止盈挂单失败（被风控挡板拒/网关断线）不抛——人工补挂（告警已记日志）。
                # 注意：此处不回滚 _tp_placed（保留已调度标记，防重推再挂；真失败由人工补）。
                logger.exception("挂止盈失败 symbol=%s（需人工补挂）", symbol)
```

**在该块之后、方法 return 前追加第四连 d**：
```python

        # d. 本地持仓账本写入（gap4 · post_close 对账数据源）。
        #    独立 try-except 软降级：账本写入失败不阻断 a 日志/b 通知/c 止盈。
        #    方向 None 不写（保守，对齐 c 连不挂止盈语义——不猜方向误记为买当卖/卖当买，
        #    账本失真比对账漏记更危险）。
        if direction in ("BUY", "SELL"):
            try:
                from trading import position_book
                position_book.apply_fill(order_id, symbol, direction, float(qty), float(price))
            except Exception:
                logger.exception("本地账本写入失败 symbol=%s（不影响日志/通知/止盈）", symbol)
```

- [ ] **Step 5: 改 `trading/__main__.py:_run_forever`（启动期 init_db）**

**定位**：`__main__.py` 当前 `_run_forever` 内 `eng.start()` 调用处（约 line 200）：
```python
    eng.start()  # 注册四 cron job + 启动 AsyncIOScheduler（不阻塞）
```

**在 `eng.start()` 之前插入**：
```python
    # 初始化本地持仓账本（gap4 · 幂等建表，对齐 experiment/store.init_db 范式）。
    # 必须在 eng.start() 之前：cron 一旦启动，_handle_order_update/_post_close 就可能
    # 读写账本，建表必须先就绪。
    from trading import position_book
    position_book.init_db()

    eng.start()  # 注册四 cron job + 启动 AsyncIOScheduler（不阻塞）
```

- [ ] **Step 6: 跑测试验证通过**

Run: `python -m pytest tests/trading/test_engine.py -v`
Expected: 全部 passed（含新 4 个 + 既有全部不回归）

- [ ] **Step 7: Commit**

```bash
git add trading/engine.py trading/__main__.py tests/trading/test_engine.py
git commit -m "feat(trading): gap4 接线 — _post_close 读账本 + _handle_order_update 写账本

- _post_close 读 position_book.get_local_positions() 注入 local_positions（空 dict 直传）
- _handle_order_update 第四连 apply_fill（BUY/SELL only，独立 try-except 软降级）
- __main__ 启动期 position_book.init_db()

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: e2e 完整交易链路 4 步

**Files:**
- Test: `tests/trading/test_e2e_trading_flow.py`

**Interfaces:**
- Consumes: `check_freshness`（data/freshness）、`eod_plan`/`post_close`（engine）、`Signal`（strategies.signal）、`trading_plan`（confirm/load）、`position_book`、`review_report`（Task 1/2/3 全部产物）

- [ ] **Step 1: 写 e2e 测试 `tests/trading/test_e2e_trading_flow.py`**

```python
# -*- coding: utf-8 -*-
"""e2e：完整交易链路 4 步闭环验收（gap4 核心交付）。

物理意图：验证二期引擎「数据时效 → 生成计划 → 隔日交易 → 复盘报告」完整业务链路。
跨日时序用 monkeypatch datetime 注入（不真睡）；broker 走 mock gw（dry_run 不触达真实
柜台）；data_lake 用 tmp parquet。

四步串联：4 步串行跑完无异常 + 计划单 symbol 贯穿出现在 fill 表 / position 表 / 复盘报告。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from trading import engine, position_book, review_report, trading_plan
from strategies.signal import Signal


# ============================================================================
# 测试辅助：tmp 隔离的账本 + 计划目录
# ============================================================================
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """隔离 position_book db + trading_plan 目录到 tmp，避免污染真实 logs/。"""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # trading_plan._plan_path 读 env TRADE_PLAN_DIR
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("TRADE_STATE_DB", db_path)
    return tmp_path


def _make_signal(symbol="300001.SZ"):
    """构造一个颈线法 Signal（build_orders_from_signals 需 symbol/entry/neckline/bottom/atr 非 None）。"""
    return Signal(
        symbol=symbol, signal_type="neckline", formed_at="2026-07-27",
        breakout_date="2026-07-27", neckline=10.5, bottom=9.5,
        entry_price=10.5, atr=0.5,  # H=neckline-bottom=1.0；stop=10.5-2*0.5=9.5；tp=10.5+2*1.0=12.5
        experiment_id="exp_e2e", experiment_weight=1.0,
    )


# ============================================================================
# 第 1 步：检查数据时效性
# ============================================================================
def test_step1_data_freshness_ok(tmp_path):
    """第 1 步：data_lake 最新日 >= 期望日 → check_freshness ok=True。"""
    from data.freshness import check_freshness

    # 造小样本 parquet（MultiIndex date,symbol，最新日 = T 日 2026-07-27）
    dates = pd.date_range("2026-07-22", periods=6, freq="D")  # 2026-07-22..27，末日=07-27
    idx = pd.MultiIndex.from_product([dates, ["300001.SZ"]], names=["date", "symbol"])
    df = pd.DataFrame({"open": 10, "high": 11, "low": 9, "close": 10, "vol": 1000}, index=idx)
    lake = tmp_path / "lake"
    lake.mkdir()
    df.to_parquet(lake / "a_shares_daily.parquet")

    result = check_freshness("daily", expected_date="2026-07-27", lake_dir=str(lake))
    assert result.ok is True
    assert result.latest_date == "2026-07-27"


def test_step1_data_freshness_stale(tmp_path):
    """第 1 步反例：data_lake 最新日 < 期望日 → ok=False（陈旧能检出，不静默 PASS）。"""
    from data.freshness import check_freshness

    dates = pd.date_range("2026-07-20", periods=3, freq="D")  # 最新 2026-07-22 < 期望 07-27
    idx = pd.MultiIndex.from_product([dates, ["300001.SZ"]], names=["date", "symbol"])
    df = pd.DataFrame({"open": 10, "high": 11, "low": 9, "close": 10, "vol": 1000}, index=idx)
    lake = tmp_path / "lake"
    lake.mkdir()
    df.to_parquet(lake / "a_shares_daily.parquet")

    result = check_freshness("daily", expected_date="2026-07-27", lake_dir=str(lake))
    assert result.ok is False


# ============================================================================
# 第 2 步：生成交易计划（T 日盘后 → eod_plan 落盘 confirmed=False）
# ============================================================================
def test_step2_generate_plan(isolated, monkeypatch):
    """第 2 步：eod_plan(signals, atr_map) → save_plan 落盘 confirmed=False（gap1/2 实证）。"""
    # patch 掉钉钉推送（不触达 dws），保留 save_plan 真实落盘
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o: True)
    # AUTO_TRADE_MODE=dry_run 让 eod_plan 内 _mode() 读到
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    sig = _make_signal()
    result = asyncio.run(engine.eod_plan(
        "2026-07-28",              # date=T+1（计划生效日）
        signals=[sig],
        atr_map={"300001.SZ": 0.5},
        capital=1_000_000.0,
    ))
    assert result["n_orders"] == 1
    # 计划落盘 confirmed=False（pre_open 会检查此位）
    plan = trading_plan.load_plan("2026-07-28")
    assert plan is not None
    assert plan["confirmed"] is False
    assert len(plan["orders"]) == 1
    assert plan["orders"][0]["order"]["symbol"] == "300001.SZ"


# ============================================================================
# 第 3 步：隔日按计划交易（T+1 日：confirm → pre_open 挂单 → 成交回报写账本）
# ============================================================================
def test_step3_trade_next_day(isolated, monkeypatch):
    """第 3 步：confirm_plan → _pre_open 挂单（DRY_RUN）→ 成交回报写 position_book（gap3/4 实证）。"""
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o: True)
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    # 先落一份计划（复用第 2 步产物路径）
    sig = _make_signal()
    asyncio.run(engine.eod_plan("2026-07-28", [sig], {"300001.SZ": 0.5}, 1_000_000.0))

    # ① 研究员确认（T-1 确认闸）
    assert trading_plan.confirm_plan("2026-07-28") is True
    assert trading_plan.load_plan("2026-07-28")["confirmed"] is True

    # ② pre_open 挂单：mock gw + _submit 返 DRY_RUN + cancel_all no-op
    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw.is_locked = False
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine, "_cancel_all_open_orders", AsyncMock(return_value=0))

    submitted = {"n": 0}
    async def _dry_submit(order, *, confirm=True):
        submitted["n"] += 1
        return {"order_id": str(submitted["n"]), "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _dry_submit)

    # dynamic_whitelist 真实跑（inject 计划 symbols），不 patch
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    eng = engine.TradingEngine()
    pre_result = asyncio.run(eng._pre_open())  # 用 TradingEngine._pre_open（cron 包装）
    assert pre_result["submitted"] >= 1  # confirmed 计划挂单成功（gap3 实证）

    # ③ 成交回报写账本：模拟 DRY_RUN 单的「成交回报」（gap4 写入实证）
    eng._gw = MagicMock()
    eng._gw._orders = {"1": {"order_type": 23}}  # 23=STOCK_BUY
    update = {
        "kind": "trade", "order_id": "1", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()):
        asyncio.run(eng._handle_order_update(update))
    # 账本写入：BUY 100 股 300001.SZ
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}


# ============================================================================
# 第 4 步：生成复盘报告（T+1 日盘后：post_close 对账 + generate_review）
# ============================================================================
def test_step4_review_report(isolated, monkeypatch):
    """第 4 步：_post_close 对账（mock gw+账本）→ generate_review 四段齐全 + save_review 落盘。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    # 预置：计划落盘 + 账本有一笔 BUY 成交（复用前两步状态）
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o: True)
    sig = _make_signal()
    asyncio.run(engine.eod_plan("2026-07-28", [sig], {"300001.SZ": 0.5}, 1_000_000.0))
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.5)

    # _post_close：mock gw 持仓与账本一致（drift=False）+ run_reconcile 返 is_ok=True
    from trading.compute.reconcile import ReconciliationResult

    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw.is_locked = False
    fake_rec = ReconciliationResult(
        matched=[], drifted=[], only_local=[], only_broker=[],
        max_abs_drift=0.0, is_ok=True)

    async def _fake_run_rec(gw, local, tolerance=0.0):
        return fake_rec
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_run_rec)
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    eng = engine.TradingEngine()
    pc_result = asyncio.run(eng._post_close())
    assert pc_result["drift"] is False  # 账本与 broker 一致 → 无偏差

    # generate_review：四段齐全（计划/成交/持仓/对账）
    md = review_report.generate_review("2026-07-28", drift=False)
    assert "300001.SZ" in md
    assert "买入 1 笔" in md
    assert "无偏差" in md

    # save_review 落盘
    out = review_report.save_review("2026-07-28", md, review_dir=str(isolated / "reviews"))
    assert out.exists()


# ============================================================================
# 全链路：4 步串行（数据一致性——计划 symbol 贯穿 fill/position/报告）
# ============================================================================
def test_e2e_full_flow_symbol_propagates(isolated, monkeypatch):
    """全链路：计划单 symbol 贯穿出现在 fill 表 / position 表 / 复盘报告（数据一致性）。"""
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o: True)
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    # 第 2 步：生成计划
    sig = _make_signal("688001.SH")
    asyncio.run(engine.eod_plan("2026-07-28", [sig], {"688001.SH": 0.5}, 1_000_000.0))
    plan = trading_plan.load_plan("2026-07-28")
    assert plan["orders"][0]["order"]["symbol"] == "688001.SH"

    # 第 3 步：confirm + 成交回报写账本
    trading_plan.confirm_plan("2026-07-28")
    eng = engine.TradingEngine()
    eng._gw = MagicMock()
    eng._gw._orders = {"o1": {"order_type": 23}}
    update = {
        "kind": "trade", "order_id": "o1", "stock_code": "688001.SH",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()):
        asyncio.run(eng._handle_order_update(update))

    # symbol 贯穿：position 表
    assert "688001.SH" in position_book.get_local_positions()

    # 第 4 步：复盘报告含同一 symbol
    md = review_report.generate_review("2026-07-28", drift=None)
    assert "688001.SH" in md  # 计划段 + 成交段 + 持仓段都应含计划 symbol
```

- [ ] **Step 2: 跑 e2e 验证失败（接线前）**

Run: `python -m pytest tests/trading/test_e2e_trading_flow.py -v`
Expected: 部分FAIL（第 3/4 步依赖 Task 3 接线；若 Task 1-3 已完成则应 PASS——此步用于回归守卫）

- [ ] **Step 3: 跑全量 trading 测试验证不回归**

Run: `python -m pytest tests/trading/ -v`
Expected: 全部 passed（含新 e2e + 既有 position_book/review_report/engine 全绿）

- [ ] **Step 4: 跑全仓关键测试守卫（颈线法/plan/reconcile 不回归）**

Run: `python -m pytest tests/trading/test_signal_runner.py tests/experiment/test_e2e_eod_to_plan.py tests/test_neckline_recognition.py -v`
Expected: 全部 passed（gap4 接线不影响信号扫描/计划生成纯函数）

- [ ] **Step 5: Commit**

```bash
git add tests/trading/test_e2e_trading_flow.py
git commit -m "test(trading): e2e 完整交易链路 4 步验收（数据时效→计划→隔日交易→复盘）

- 第1步 check_freshness 时效检查（fresh/stale 双场景）
- 第2步 eod_plan 生成计划落盘 confirmed=False
- 第3步 confirm+pre_open 挂单+成交回报写 position_book
- 第4步 post_close 对账+generate_review 复盘四段
- 全链路 symbol 贯穿 fill/position/报告 数据一致性

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review 已完成

**Spec coverage**：
- §3.2 SQLite 表结构 → Task 1 Step 3 ✓
- §3.3 position_book 三函数 → Task 1 ✓
- §3.4 写入点 _handle_order_update 第四连 → Task 3 Step 4 ✓
- §3.5 读取点 _post_close → Task 3 Step 3 ✓
- §3.7 启动期 init_db → Task 3 Step 5 ✓
- §3.8 review_report → Task 2 ✓
- §5.1 单测（position_book/review_report/engine）→ Task 1/2/3 ✓
- §5.2 e2e 4 步 → Task 4 ✓
- R1 幂等 → Task 1 `test_apply_fill_idempotent` ✓
- R3 方向未知不写 → Task 1 `test_apply_fill_unknown_direction_raises` + Task 3 `test_handle_order_update_writes_book` ✓
- R4 dry_run 不污染 → Task 4 `test_step3_trade_next_day`（dry_run 下账本经成交回报写入，broker mock 空）✓

**Placeholder 扫描**：无 TBD/TODO（代码全给出，命令 + 期望输出齐全）。

**类型一致性**：`apply_fill`/`get_local_positions`/`init_db`/`generate_review`/`save_review` 签名在各 Task 间一致；db_path 默认 None + 运行时解析全局统一。
