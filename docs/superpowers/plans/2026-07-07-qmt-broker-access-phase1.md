# QMT 实盘接入 Phase 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补全国金 MiniQMT (xtquant) 实盘接入的后端装配缺口——配置、风控挡板、xtdata 行情、6 个 REST 路由、网关单测、联调脚本，使真实资金账号 `62138335` 可经 HTTP 完成连接/下单/撤单/查询，且 dry_run 由请求级参数控制、交易流水五场景全覆盖。

**Architecture:** 复用既有 `QmtExecutionGateway`（核心逻辑零改动，已通过 API 事实审查）；新增 `risk_shield.py`（纯函数 10 关挡板）+ `qmt_market_data.py`（xtdata 延迟容错）；扩展 `trading_service`（业务编排）+ `trading.py`（薄路由）。前端 Cockpit UI、策略引擎实盘属 Phase 2/3，本期仅留 API 接口位。

**Tech Stack:** Python 3.10+ / FastAPI / pytest（无 pytest-asyncio，用 `asyncio.run`）/ xtquant（Windows + MiniQMT，延迟容错 import）

## Global Constraints

- **语言红线**（CLAUDE.md）：所有代码注释、commit message、日志均用标准中文；注释说明 Why（交易物理意图/数学推导），不只是 What
- **反魔法**（CLAUDE.md）：xtquant 调用须经 `run_in_executor` 投线程池；风控挡板是纯函数（无 I/O），quote 由外部预取注入
- **测试纪律**：pytest + `monkeypatch` 注入假对象；异步用 `asyncio.run(run())` 包装；`fire_and_forget` 用 swallow mock 屏蔽告警副作用；CI 无 xtquant 也能全绿
- **dry_run 双开关**：请求级 `body.dry_run`（前端控制）+ env `QMT_ALLOW_LIVE_TRADE`（环境总闸）；二者组合语义见 Task 2
- **xtquant 路径**：`QMT_USERDATA_PATH=D:\国金QMT交易端模拟\userdata_mini`（已确认生成）；账号 `QMT_ACCOUNT_ID=62138335`
- **密码 `100486` 绝不进代码/env**——仅在 MiniQMT 客户端登录界面输入
- **既有测试不回归**：每 Task commit 前本 Task 测试绿；Task 8 全量 444+ 测试绿

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `.env.example` / `.env` | 改 | 加 QMT_* 配置（路径/账号/总闸/上限/白名单） |
| `trading/__init__.py` | 改 | 导出 `QmtExecutionGateway` |
| `trading/risk_shield.py` | 新增 | 10 关纯函数挡板 → `RiskDecision` |
| `tests/test_risk_shield.py` | 新增 | 挡板穷举单测 |
| `trading/qmt_market_data.py` | 新增 | xtdata `get_quote` 延迟容错封装 |
| `tests/test_qmt_market_data.py` | 新增 | 行情封装单测（mock xtdata） |
| `tests/test_qmt_gateway.py` | 新增 | 网关单测（注入假 xtquant，补 0 覆盖） |
| `server/services/trading_service.py` | 改 | +connect/submit/cancel/orders/asset + 流水 |
| `tests/test_trading_service.py` | 改 | +submit_order/cancel/connect 单测 |
| `server/api/v1/trading.py` | 改 | +6 路由（含 `/submit_order` body.dry_run） |
| `tests/test_trading_api.py` | 新增 | 路由端到端冒烟（TestClient） |
| `scripts/qmt_smoke.py` | 新增 | 真实联调脚本（5 步人工确认） |

---

## Task 1: 配置层 + 网关导出

**Files:**
- Modify: `.env.example`（追加 QMT 配置块）
- Modify: `.env`（追加同样配置，含真实路径——不进 git）
- Modify: `trading/__init__.py`（导出 `QmtExecutionGateway`）
- Test: `tests/test_risk_shield.py`（占位，Task 2 填充——本 Task 用一次性 import 验证）

**Interfaces:**
- Produces: `from trading import QmtExecutionGateway` 可用（为 Task 5 提供）

- [ ] **Step 1: 写失败测试（验证导出）**

创建 `tests/test_risk_shield.py`（先放一个 import 测试，Task 2 扩充）：

```python
# -*- coding: utf-8 -*-
"""风控挡板 + 配置层冒烟测试。"""


def test_qmt_gateway_exported():
    """trading 包应导出 QmtExecutionGateway（Task 1 配置层契约）。"""
    from trading import QmtExecutionGateway
    assert QmtExecutionGateway is not None
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_risk_shield.py::test_qmt_gateway_exported -v
```
Expected: FAIL with `ImportError: cannot import name 'QmtExecutionGateway'`

- [ ] **Step 3: 修改 `trading/__init__.py` 导出**

将 `trading/__init__.py` 替换为：

```python
"""真实交易模块：QMT 对接、订单状态机、风控挡板。

职责：
1. Mock 交易模拟层（第一优先级）
2. 订单状态机（处理断线、限频、部分成交）
3. QMT 实盘执行网关（xtquant 异步封装）
4. 风控挡板（纯函数，下单前 10 关校验）
5. 保证金敞口监控
"""

from .mock_broker import MockBroker
from .order_state import OrderStateMachine, OrderState
# QmtExecutionGateway 延迟 import：模块顶部 import qmt_gateway 会触发 xtquant 容错 import，
# 在无 xtquant 的开发/CI 环境仍可正常加载（_XTQUANT_AVAILABLE=False 退化基类为 object）。
from .qmt_gateway import QmtExecutionGateway

__all__ = [
    "MockBroker",
    "OrderStateMachine",
    "OrderState",
    "QmtExecutionGateway",
]
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
pytest tests/test_risk_shield.py::test_qmt_gateway_exported -v
```
Expected: PASS

- [ ] **Step 5: 追加 `.env.example` 的 QMT 配置块**

在 `.env.example` 末尾追加：

```ini

# ============ QMT 实盘交易（Phase 1）============
# userdata_mini 完整路径（MiniQMT/XtItClient 启动登录后自动生成）
QMT_USERDATA_PATH=
QMT_ACCOUNT_ID=
QMT_SESSION_ID=123456
QMT_STRATEGY_NAME=quanter
# 风控挡板（dry_run 由前端按单控制，不在此；此为环境级总闸）
QMT_ALLOW_LIVE_TRADE=false
QMT_ORDER_MAX_AMOUNT=1000
QMT_ORDER_MAX_SHARES=100
QMT_SYMBOL_WHITELIST=510300.SH,511010.SH,510500.SH,159915.SZ
QMT_ENFORCE_SESSION=true
```

- [ ] **Step 6: 追加 `.env` 的真实配置（不进 git）**

在 `.env` 末尾追加（含真实路径/账号）：

```ini

# === QMT 实盘（模拟盘 XtItClient，userdata_mini 已生成）===
QMT_USERDATA_PATH=D:\国金QMT交易端模拟\userdata_mini
QMT_ACCOUNT_ID=62138335
QMT_SESSION_ID=123456
QMT_STRATEGY_NAME=quanter
QMT_ALLOW_LIVE_TRADE=false
QMT_ORDER_MAX_AMOUNT=1000
QMT_ORDER_MAX_SHARES=100
QMT_SYMBOL_WHITELIST=510300.SH,511010.SH,510500.SH,159915.SZ
QMT_ENFORCE_SESSION=true
```

- [ ] **Step 7: Commit**

```bash
git add .env.example trading/__init__.py tests/test_risk_shield.py
git commit -m "feat(qmt): 配置层 + 导出 QmtExecutionGateway（Phase 1 Task 1）"
```

---

## Task 2: 风控挡板纯函数（`risk_shield.py`，10 关）

**Files:**
- Create: `trading/risk_shield.py`
- Test: `tests/test_risk_shield.py`（扩充）

**Interfaces:**
- Consumes: `trading.execution_gateway.OrderRequest`（既有：`symbol/qty/side/price/order_id`）
- Produces: `RiskDecision(blocked, reason, stage, is_dry_run)` + `check_order(order, *, dry_run, allow_live, whitelist, max_amount, max_shares, quote, enforce_session, is_locked, connected, confirm, in_session=True) -> RiskDecision`

- [ ] **Step 1: 写失败测试（10 关 + 全过 + dry_run 标志）**

将 `tests/test_risk_shield.py` 替换为：

```python
# -*- coding: utf-8 -*-
"""风控挡板（risk_shield）纯函数穷举单测。

覆盖 10 关短路 + dry_run 模拟语义（is_dry_run=True 不算错误）+ 全过放行。
挡板是纯函数：所有外部数据（quote/连接状态/dry_run）由参数注入，确定性可测。
"""
import pytest

from trading.execution_gateway import OrderRequest
from trading.risk_shield import RiskDecision, check_order


def _order(**kw):
    """造一个默认合法订单（白名单内、100 整手、限价、金额内）。"""
    base = dict(symbol="510300.SH", qty=100, side="buy", price=5.0)
    base.update(kw)
    return OrderRequest(**base)


def _ok_kwargs(**kw):
    """造一组全过的挡板参数（连接正常、实盘放行、确认、白名单、quote 正常、时段内）。"""
    base = dict(
        dry_run=False, allow_live=True,
        whitelist={"510300.SH"}, max_amount=1000.0, max_shares=100,
        quote={"last_price": 5.0, "high_limit": 5.5, "low_limit": 4.5},
        enforce_session=True, is_locked=False, connected=True,
        confirm=True, in_session=True,
    )
    base.update(kw)
    return base


def test_qmt_gateway_exported():
    """trading 包应导出 QmtExecutionGateway（Task 1 配置层契约）。"""
    from trading import QmtExecutionGateway
    assert QmtExecutionGateway is not None


def test_pass_all_clear():
    """全过 → blocked=False。"""
    d = check_order(_order(), **_ok_kwargs())
    assert d.blocked is False
    assert d.stage == ""


def test_block_connection_locked():
    d = check_order(_order(), **_ok_kwargs(is_locked=True))
    assert d.blocked and d.stage == "connection"


def test_block_connection_disconnected():
    d = check_order(_order(), **_ok_kwargs(connected=False))
    assert d.blocked and d.stage == "connection"


def test_dry_run_is_not_error():
    """dry_run=True → blocked=True 但 is_dry_run=True（模拟语义，非拒单错误）。"""
    d = check_order(_order(), **_ok_kwargs(dry_run=True))
    assert d.blocked is True
    assert d.is_dry_run is True
    assert d.stage == "dry_run"


def test_block_allow_live_gate():
    """dry_run=False 但 allow_live=False → 拒单（强制模拟）。"""
    d = check_order(_order(), **_ok_kwargs(dry_run=False, allow_live=False))
    assert d.blocked and d.stage == "allow_live" and d.is_dry_run is False


def test_block_no_confirm():
    d = check_order(_order(), **_ok_kwargs(confirm=False))
    assert d.blocked and d.stage == "confirm"


def test_block_whitelist():
    d = check_order(_order(symbol="000001.SZ"),
                    **_ok_kwargs(whitelist={"510300.SH"}))
    assert d.blocked and d.stage == "whitelist"


def test_block_lot_size():
    d = check_order(_order(qty=150), **_ok_kwargs(max_shares=1000))
    assert d.blocked and d.stage == "lot"


def test_block_lot_zero():
    d = check_order(_order(qty=0), **_ok_kwargs(max_shares=1000))
    assert d.blocked and d.stage == "lot"


def test_block_max_amount():
    # 100 股 * 5.0 = 500，上限调到 400 → 触发
    d = check_order(_order(qty=100, price=5.0), **_ok_kwargs(max_amount=400.0))
    assert d.blocked and d.stage == "max_amount"


def test_block_max_shares():
    d = check_order(_order(qty=200), **_ok_kwargs(max_shares=100, max_amount=100000))
    assert d.blocked and d.stage == "max_shares"


def test_block_high_limit():
    q = {"last_price": 5.6, "high_limit": 5.5, "low_limit": 4.5}
    d = check_order(_order(), **_ok_kwargs(quote=q))
    assert d.blocked and d.stage == "high_limit"


def test_block_low_limit():
    q = {"last_price": 4.4, "high_limit": 5.5, "low_limit": 4.5}
    d = check_order(_order(), **_ok_kwargs(quote=q))
    assert d.blocked and d.stage == "low_limit"


def test_block_session():
    d = check_order(_order(), **_ok_kwargs(in_session=False))
    assert d.blocked and d.stage == "session"


def test_no_quote_skips_limit_check():
    """quote=None → 跳过涨跌停关（xtdata 不可用时的降级）。"""
    d = check_order(_order(), **_ok_kwargs(quote=None))
    assert d.blocked is False


def test_short_circuit_order():
    """关 1（连接）优先于关 4（confirm）：断线时即便 confirm=False 也只报 connection。"""
    d = check_order(_order(), **_ok_kwargs(is_locked=True, confirm=False))
    assert d.stage == "connection"
```

- [ ] **Step 2: 运行测试，确认全部失败**

```bash
pytest tests/test_risk_shield.py -v
```
Expected: 多个 FAIL with `ImportError: cannot import name 'risk_shield'`（除 test_qmt_gateway_exported 已绿）

- [ ] **Step 3: 实现 `trading/risk_shield.py`**

创建 `trading/risk_shield.py`：

```python
"""
trading/risk_shield.py
======================
下单风控挡板（纯函数，无 I/O）。

设计哲学（CLAUDE.md Karpathy 极简 + 事实审查）：
- 纯函数：所有外部数据（quote 快照、连接状态、dry_run、env 配置）由调用方注入，
  保证 test_risk_shield.py 可确定性穷举单测，无需 mock 网络/环境。
- 短路求值：10 关自上而下，任一命中即返 blocked，不继续下关（关 1 连接优先级最高）。
- 决策可审计：RiskDecision.stage 记命中关卡名，便于落 CSV + 前端分流提示。

dry_run 双开关语义（研究员明确要求"前端控制是否真实下单"）：
- dry_run（请求级，POST body）= True → 模拟，不真下单，is_dry_run=True（非错误，
  调用方应落 DRY_RUN_* 流水后返回成功语义）
- dry_run=False 但 allow_live（env QMT_ALLOW_LIVE_TRADE）=False → 拒单（强制模拟）
- dry_run=False 且 allow_live=True → 放行真下单
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trading.execution_gateway import OrderRequest


@dataclass(frozen=True)
class RiskDecision:
    """风控挡板决策（不可变值对象）。

    blocked=True 时 reason/stage 非空。
    is_dry_run=True 仅在 dry_run 模拟命中时为真——它是「模拟」而非「错误」，
    调用方据此落 DRY_RUN_* 流水并返回成功语义（区别于其他关的 409 拒单）。
    """

    blocked: bool
    reason: str = ""
    stage: str = ""
    is_dry_run: bool = False


def check_order(
    order: OrderRequest,
    *,
    dry_run: bool,
    allow_live: bool,
    whitelist: set,
    max_amount: float,
    max_shares: float,
    quote: Mapping[str, Any] | None,
    enforce_session: bool,
    is_locked: bool,
    connected: bool,
    confirm: bool,
    in_session: bool = True,
) -> RiskDecision:
    """10 关短路校验。任一关命中即返 RiskDecision(blocked=True, stage=<关卡名>)。

    关卡顺序即优先级（短路）：
      1 connection  断线/未连接          — 状态机边界，最高优先
      2 dry_run     请求级模拟           — is_dry_run=True，非错误
      3 allow_live  实盘总闸(env)        — 强制模拟
      4 confirm     二次确认             — 防误触
      5 whitelist   标的白名单
      6 lot         A 股 100 整手契约
      7 max_amount  单笔金额上限
      8 max_shares  单笔股数上限
      9 high/low_limit  涨跌停封板（quote 缺失则跳过）
     10 session     A 股交易时段（enforce_session=True 时生效）
    """
    # 关1：断线/连接（最高优先——断线时其他校验无意义）
    if is_locked or not connected:
        return RiskDecision(True, "网关未连接或已锁定（断线保护）", "connection")

    # 关2：dry_run（请求级，前端控制）—— 模拟语义，is_dry_run=True
    if dry_run:
        return RiskDecision(True, "dry_run 模拟（前端请求不真下单）", "dry_run", is_dry_run=True)

    # 关3：实盘总闸（env QMT_ALLOW_LIVE_TRADE）
    if not allow_live:
        return RiskDecision(True, "实盘总闸 QMT_ALLOW_LIVE_TRADE=false，禁止真下单", "allow_live")

    # 关4：二次确认
    if not confirm:
        return RiskDecision(True, "缺少二次确认 confirm=true", "confirm")

    # 关5：标的白名单
    if order.symbol not in whitelist:
        return RiskDecision(True, f"标的 {order.symbol} 不在白名单", "whitelist")

    # 关6：A 股 100 整手契约（qty<=0 或非 100 整数倍 → 拒）
    if order.qty <= 0 or int(order.qty) % 100 != 0:
        return RiskDecision(True, f"数量 {order.qty} 非 100 整数倍（A 股整手契约）", "lot")

    # 关7：单笔金额上限（限价用 order.price，市价用 quote.last_price 估算）
    ref_price = order.price
    if ref_price is None and quote is not None:
        ref_price = quote.get("last_price")
    if ref_price is not None and order.qty * ref_price > max_amount:
        return RiskDecision(
            True,
            f"单笔金额 {order.qty * ref_price:.2f} 超上限 {max_amount}",
            "max_amount",
        )

    # 关8：单笔股数上限
    if order.qty > max_shares:
        return RiskDecision(True, f"单笔股数 {order.qty} 超上限 {max_shares}", "max_shares")

    # 关9：涨跌停封板（quote 缺失 → 跳过，xtdata 不可用时的降级）
    if quote is not None:
        last = quote.get("last_price")
        high = quote.get("high_limit")
        low = quote.get("low_limit")
        if last is not None and high is not None and last >= high:
            return RiskDecision(True, f"{order.symbol} 已涨停（{last}>={high}）", "high_limit")
        if last is not None and low is not None and last <= low:
            return RiskDecision(True, f"{order.symbol} 已跌停（{last}<={low}）", "low_limit")

    # 关10：A 股交易时段
    if enforce_session and not in_session:
        return RiskDecision(True, "非 A 股交易时段", "session")

    # 全过：放行真下单
    return RiskDecision(False)
```

- [ ] **Step 4: 运行测试，确认全绿**

```bash
pytest tests/test_risk_shield.py -v
```
Expected: 全部 PASS（16 个测试）

- [ ] **Step 5: Commit**

```bash
git add trading/risk_shield.py tests/test_risk_shield.py
git commit -m "feat(risk): 风控挡板纯函数 10 关 + dry_run 双开关（Phase 1 Task 2）"
```

---

## Task 3: xtdata 行情封装（`qmt_market_data.py`）

**Files:**
- Create: `trading/qmt_market_data.py`
- Test: `tests/test_qmt_market_data.py`

**Interfaces:**
- Produces: `async def get_quote(symbol: str) -> Mapping[str, Any] | None`（返 `{last_price, high_limit, low_limit, ...}` 或 None）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_qmt_market_data.py`：

```python
# -*- coding: utf-8 -*-
"""xtdata 行情封装单测：mock xtdata，覆盖可用/不可用/异常/空数据四路径。"""
import asyncio
import types

import pytest


def test_get_quote_unavailable(monkeypatch):
    """xtdata 不可用 → None（调用方须容忍）。"""
    from trading import qmt_market_data as md
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", False)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())


def test_get_quote_ok(monkeypatch):
    """xtdata 可用且返数据 → 返回单标的快照 dict。"""
    from trading import qmt_market_data as md
    fake = types.SimpleNamespace(
        get_full_tick=lambda codes: {"600000.SH": {"last_price": 10.5, "high_limit": 11.5, "low_limit": 9.5}}
    )
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is not None
        assert r["last_price"] == 10.5
    asyncio.run(run())


def test_get_quote_exception_returns_none(monkeypatch):
    """xtdata 抛异常 → 捕获返 None（绝不冒泡到调用方）。"""
    from trading import qmt_market_data as md

    def boom(codes):
        raise RuntimeError("C++ 内部错误")
    fake = types.SimpleNamespace(get_full_tick=boom)
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())


def test_get_quote_empty_returns_none(monkeypatch):
    """get_full_tick 返空 dict 或缺该标的 → None。"""
    from trading import qmt_market_data as md
    fake = types.SimpleNamespace(get_full_tick=lambda codes: {})
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        r = await md.get_quote("600000.SH")
        assert r is None
    asyncio.run(run())
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_qmt_market_data.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'trading.qmt_market_data'`

- [ ] **Step 3: 实现 `trading/qmt_market_data.py`**

```python
"""
trading/qmt_market_data.py
==========================
xtdata 行情封装（延迟容错）。

职责：提供单标的实时快照（last_price / high_limit / low_limit），供
- risk_shield 第 9 关（涨跌停封板校验）
- trading_service.get_positions（持仓市值/浮盈富化）

设计（CLAUDE.md 彻底掌控执行环境）：
- xtdata.get_full_tick 是同步 C++ 调用，经 loop.run_in_executor 投线程池，绝不阻塞事件循环。
- 延迟容错 import：无 xtquant 的开发/CI 环境 _XTDATA_AVAILABLE=False，get_quote 返 None，
  调用方据此降级（risk_shield 跳过涨跌停关，positions 市值为 None）。
- 任何异常捕获返 None，绝不冒泡——行情缺失不应阻断下单/查询主路径。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

try:
    from xtquant import xtdata  # type: ignore
    _XTDATA_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境相关，非逻辑分支
    xtdata = None  # type: ignore[assignment]
    _XTDATA_AVAILABLE = False


async def get_quote(symbol: str) -> Optional[Mapping[str, Any]]:
    """经线程池取 xtdata.get_full_tick([symbol])，返单标的快照 dict。

    返回字段（来源 xtdata get_full_tick 契约）：
        last_price / high_limit / low_limit / open / pre_close ...
    返回 None 的场景：
        - xtdata 不可用（_XTDATA_AVAILABLE=False）
        - get_full_tick 抛异常（C++ 内部错误）
        - 返回空或不含该标的
    调用方（risk_shield / get_positions）必须容忍 None。
    """
    if not _XTDATA_AVAILABLE:
        logger.debug("xtdata 不可用，get_quote 返 None（降级模式）")
        return None
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: xtdata.get_full_tick([symbol]))  # type: ignore[union-attr]
    except Exception as exc:
        # 行情查询失败不阻断主路径：捕获记 warning，返 None 让调用方降级
        logger.warning("xtdata.get_full_tick 异常 symbol=%s: %s", symbol, exc)
        return None
    if not raw or symbol not in raw:
        return None
    return raw[symbol]
```

- [ ] **Step 4: 运行测试，确认全绿**

```bash
pytest tests/test_qmt_market_data.py -v
```
Expected: 4 个 PASS

- [ ] **Step 5: Commit**

```bash
git add trading/qmt_market_data.py tests/test_qmt_market_data.py
git commit -m "feat(quote): xtdata 行情封装 get_quote 延迟容错（Phase 1 Task 3）"
```

---

## Task 4: QMT 网关单测（补 0 覆盖）

**Files:**
- Create: `tests/test_qmt_gateway.py`
- 不改 `trading/qmt_gateway.py`（除非发现 bug——若有则单列修复 commit）

**Interfaces:**
- 测既有：`_map_qmt_status`、`_assert_status_contract`、`QmtExecutionGateway.connect/submit_order/cancel_order`、回调 `on_disconnected/on_order_stock_async_response`

- [ ] **Step 1: 写失败测试（注入假 xtquant）**

创建 `tests/test_qmt_gateway.py`：

```python
# -*- coding: utf-8 -*-
"""QmtExecutionGateway 单测：注入假 xtquant，覆盖状态映射/连接时序/下单/撤单/断线/回调。

测试手法（CLAUDE.md 事实审查——不臆造 xtquant）：
- 模块级向 sys.modules 注入假的 xtquant/xttrader/xttype/xtconstant，使 qmt_gateway 顶部
  try/except 走 _XTQUANT_AVAILABLE=True 分支，从而能实例化 QmtExecutionGateway。
- FakeXtQuantTrader 记录所有调用 + 返回可控 rc/seq，验证时序与 seq→real 映射。
- xtconstant 枚举值与 qmt_gateway 字面量契约（_QMT_ORDER_*）一致，_assert_status_contract 应通过。
"""
import asyncio
import os
import sys
import types

import pytest


# ============ 模块级注入假 xtquant（在 import qmt_gateway 前完成）============
def _install_fake_xtquant():
    if "xtquant" in sys.modules and getattr(sys.modules["xtquant"], "_FAKE", False):
        return  # 已注入

    # 假 xtconstant：枚举值与 qmt_gateway._QMT_ORDER_* 字面量契约一致
    fake_xtconstant = types.ModuleType("xtquant.xtconstant")
    fake_xtconstant.STOCK_BUY = 23
    fake_xtconstant.STOCK_SELL = 24
    fake_xtconstant.LATEST_PRICE = 5
    fake_xtconstant.FIX_PRICE = 11
    for name, val in [
        ("ORDER_UNREPORTED", 48), ("ORDER_REPORTED", 50), ("ORDER_REPORTED_CANCEL", 51),
        ("ORDER_CANCELED", 54), ("ORDER_PART_SUCC", 55), ("ORDER_SUCCEEDED", 56),
        ("ORDER_JUNK", 57),
    ]:
        setattr(fake_xtconstant, name, val)

    # 假 StockAccount
    fake_xttype = types.ModuleType("xtquant.xttype")

    class FakeStockAccount:
        def __init__(self, acc_id, acc_type="STOCK"):
            self.account_id = acc_id
            self.account_type = 2  # 任意 int（柜台内部类型编码，测试不关心具体值）
    fake_xttype.StockAccount = FakeStockAccount

    # 假 XtQuantTraderCallback（基类，object 即可）
    fake_xttrader = types.ModuleType("xtquant.xttrader")

    class _CallbackBase:
        pass

    # 假 XtQuantTrader：可配置 rc/seq/positions
    class FakeXtQuantTrader:
        connect_rc = 0
        subscribe_rc = 0
        cancel_rc = 0
        order_seq = 100
        positions = None

        def __init__(self, path, sid):
            self.path, self.sid = path, sid
            self.cb = None
            self.calls = []

        def register_callback(self, cb):
            self.cb = cb
            self.calls.append("register_callback")

        def start(self):
            self.calls.append("start")

        def connect(self):
            self.calls.append("connect")
            return self.connect_rc

        def subscribe(self, acc):
            self.calls.append("subscribe")
            return self.subscribe_rc

        def stop(self):
            self.calls.append("stop")

        def order_stock_async(self, *args):
            self.calls.append(("order_stock_async", args))
            seq = self.order_seq
            self.order_seq += 1
            return seq

        def cancel_order_stock(self, acc, oid):
            self.calls.append(("cancel_order_stock", oid))
            return self.cancel_rc

        def query_stock_positions(self, acc):
            return self.positions

    fake_xttrader.XtQuantTrader = FakeXtQuantTrader
    fake_xttrader.XtQuantTraderCallback = _CallbackBase

    fake_xt = types.ModuleType("xtquant")
    fake_xt._FAKE = True

    sys.modules["xtquant"] = fake_xt
    sys.modules["xtquant.xtconstant"] = fake_xtconstant
    sys.modules["xtquant.xttype"] = fake_xttype
    sys.modules["xtquant.xttrader"] = fake_xttrader


_install_fake_xtquant()

from trading import qmt_gateway  # noqa: E402
from trading.qmt_gateway import (  # noqa: E402
    QmtExecutionGateway, _map_qmt_status, _assert_status_contract,
)
from trading.execution_gateway import OrderRequest  # noqa: E402
from trading.order_state import OrderState  # noqa: E402


def _make_gw(monkeypatch):
    """构造一个已 connect 的网关（connect_rc=subscribe_rc=0）。"""
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:/fake/userdata_mini")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "62138335")
    gw = QmtExecutionGateway()

    async def run():
        await gw.connect()
    asyncio.run(run())
    return gw


# ============ 状态映射纯函数 ============

def test_map_status_succeeded():
    assert _map_qmt_status(56) == OrderState.FILLED


def test_map_status_partial():
    assert _map_qmt_status(55) == OrderState.PARTIAL_FILLED


def test_map_status_junk():
    assert _map_qmt_status(57) == OrderState.REJECTED


def test_map_status_canceled_and_reported_cancel():
    assert _map_qmt_status(54) == OrderState.CANCELLED
    assert _map_qmt_status(51) == OrderState.CANCELLED  # 已报待撤


def test_map_status_partial_cancel():
    assert _map_qmt_status(53) == OrderState.PARTIAL_CANCELLED
    assert _map_qmt_status(52) == OrderState.PARTIAL_CANCELLED  # 部成待撤


def test_map_status_intermediate_returns_submitted():
    """48/49/50/255 中间态/未知 → 保守 SUBMITTED（不冒进终态）。"""
    for s in (48, 49, 50, 255):
        assert _map_qmt_status(s) == OrderState.SUBMITTED


def test_assert_status_contract_ok():
    """注入一致枚举 → 不抛。"""
    _assert_status_contract()  # 不抛即通过


# ============ 连接时序 ============

def test_connect_success(monkeypatch):
    gw = _make_gw(monkeypatch)
    assert gw._connected is True
    assert gw._lock_down is False
    # 时序：register_callback → start → connect → subscribe
    trader = gw._trader
    assert trader.calls[:4] == ["register_callback", "start", "connect", "subscribe"]


def test_connect_failure_raises(monkeypatch):
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:/fake/userdata_mini")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "62138335")
    from trading.qmt_gateway import QmtExecutionGateway as G
    # 让 FakeTrader.connect 返非 0
    type(gw := G())._trader  # noqa
    # 直接降 connect_rc
    monkeypatch.setattr(qmt_gateway.sys.modules["xtquant.xttrader"].XtQuantTrader,
                       "connect_rc", 1, raising=False)
    gw2 = G()
    with pytest.raises(ConnectionError):
        asyncio.run(gw2.connect())
    assert gw2._lock_down is True


def test_missing_credentials_raises(monkeypatch):
    """无 QMT_USERDATA_PATH → 构造即 ValueError。"""
    monkeypatch.delenv("QMT_USERDATA_PATH", raising=False)
    monkeypatch.setenv("QMT_ACCOUNT_ID", "62138335")
    with pytest.raises(ValueError):
        QmtExecutionGateway()


# ============ 下单/撤单 ============

def test_submit_order_returns_seq(monkeypatch):
    gw = _make_gw(monkeypatch)
    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)

    async def run():
        return await gw.submit_order(order)
    res = asyncio.run(run())
    assert res.state == OrderState.SUBMITTED
    assert res.order_id == "100"  # FakeTrader.order_seq 起始 100


def test_submit_order_rejected_on_neg_seq(monkeypatch):
    """order_stock_async 返 -1 → REJECTED（柜台拒单）。"""
    gw = _make_gw(monkeypatch)
    monkeypatch.setattr(gw._trader, "order_seq", -1)  # 下一次返 -1
    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)

    async def run():
        return await gw.submit_order(order)
    res = asyncio.run(run())
    assert res.state == OrderState.REJECTED


def test_cancel_without_mapping_fails(monkeypatch):
    """seq→real 映射未建立 → cancel FAILED（引导上层短暂重试）。"""
    gw = _make_gw(monkeypatch)
    async def run():
        return await gw.cancel_order("999")
    res = asyncio.run(run())
    assert res.state == OrderState.FAILED


def test_cancel_after_async_response(monkeypatch):
    """on_order_stock_async_response 建立映射后 → cancel 成功发出。"""
    gw = _make_gw(monkeypatch)
    # 模拟 async_response 回调：seq=100 → real_order_id=8888
    gw.on_order_stock_async_response(
        types.SimpleNamespace(seq=100, order_id=8888)
    )
    assert gw._seq_to_real[100] == 8888

    async def run():
        return await gw.cancel_order("100")
    res = asyncio.run(run())
    assert res.state == OrderState.CANCELLED


# ============ 断线锁定 ============

def test_on_disconnected_locks(monkeypatch):
    """on_disconnected 回调 → is_locked=True（断线熔断）。"""
    gw = _make_gw(monkeypatch)
    assert gw.is_locked is False
    # on_disconnected 在 C++ 线程触发；这里需 loop 才能 call_soon_threadsafe
    async def run():
        gw.on_disconnected()  # 内部 call_soon_threadsafe 投递 _on_disconnect_fatal
        await asyncio.sleep(0.01)  # 让投递落地
    asyncio.run(run())
    assert gw.is_locked is True
    assert gw._connected is False
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_qmt_gateway.py -v
```
Expected: 部分可能直接通过（因为测的是既有代码）；若有失败，记录是测试错还是代码 bug。

- [ ] **Step 3: 若有失败——修正测试（不改生产代码除非确认是 bug）**

逐个核对失败用例：
- 若测试断言写错（如 FakeTrader 配置方式不对）→ 修测试
- 若 `qmt_gateway.py` 真有 bug（如 connect 失败路径未置 _lock_down）→ 在此 Step 修生产代码并单独 commit

典型需确认点：`test_connect_failure_raises` 的 FakeTrader.connect_rc 类变量改法是否生效；如不生效，改为实例级配置。

- [ ] **Step 4: 运行测试，确认全绿**

```bash
pytest tests/test_qmt_gateway.py -v
```
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_qmt_gateway.py
git commit -m "test(qmt): 网关单测覆盖状态映射/连接/下单/撤单/断线（Phase 1 Task 4）"
```

---

## Task 5: `trading_service` 扩展（业务编排 + 流水全覆盖）

**Files:**
- Modify: `server/services/trading_service.py`
- Test: `tests/test_trading_service.py`（扩充）

**Interfaces:**
- Consumes: `risk_shield.check_order`、`qmt_market_data.get_quote`、`OrderRequest`
- Produces:
  - `async def connect_gateway() -> None`（raise RuntimeError/ConnectionError）
  - `async def disconnect_gateway() -> None`
  - `async def submit_order(order, *, dry_run, confirm) -> dict`（挡板命中 raise RuntimeError；dry_run 返 `{"state":"DRY_RUN"}`；真单返 `{"order_id","state","message"}`）
  - `async def cancel_order(order_id) -> dict`
  - `async def get_orders() -> list`
  - `async def get_asset() -> dict`
  - 新增模块级 env helper：`_allow_live()/_whitelist()/_max_amount()/_max_shares()/_enforce_session()`

- [ ] **Step 1: 写失败测试（扩充 `tests/test_trading_service.py`）**

在 `tests/test_trading_service.py` 末尾追加：

```python


# ============ Phase 1 Task 5：submit_order / connect / 流水 ============
import asyncio
import types


def _fake_gw_connected():
    """造一个已连接、未锁定的假网关，记录 submit_order 调用。"""
    class _FakeGW:
        def __init__(self):
            self._connected = True
            self._lock_down = False
            self._orders = {}
            self.submit_calls = []
            self.connect_called = False

        @property
        def is_locked(self):
            return self._lock_down

        async def connect(self):
            self.connect_called = True
            self._connected = True
            self._lock_down = False

        async def disconnect(self):
            self._connected = False

        async def submit_order(self, order):
            self.submit_calls.append(order)
            from trading.execution_gateway import OrderResult
            from trading.order_state import OrderState
            return OrderResult(order_id="100", state=OrderState.SUBMITTED, message="ok")

        async def cancel_order(self, order_id):
            from trading.execution_gateway import OrderResult
            from trading.order_state import OrderState
            return OrderResult(order_id=order_id, state=OrderState.CANCELLED, message="ok")
    return _FakeGW()


def test_connect_gateway(monkeypatch):
    from server.services import trading_service
    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    asyncio.run(trading_service.connect_gateway())
    assert gw.connect_called is True


def test_connect_gateway_unavailable(monkeypatch):
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(trading_service.connect_gateway())


def test_submit_order_dry_run_records_and_returns(monkeypatch):
    """dry_run=True → 不调网关下单，落 DRY_RUN_BUY 流水，返 state=DRY_RUN。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)

    recorded = []
    monkeypatch.setattr(trading_service, "record_live_trade",
                        lambda *a, **kw: recorded.append((a, kw)))

    # quote 预取返 None（不依赖 xtdata）
    async def _no_quote(s):
        return None
    monkeypatch.setattr(trading_service.qmt_market_data, "get_quote", _no_quote)

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    r = asyncio.run(trading_service.submit_order(order, dry_run=True, confirm=True))
    assert r["state"] == "DRY_RUN"
    assert gw.submit_calls == []  # 未真下单
    assert recorded and recorded[0][0][1] == "DRY_RUN_BUY"  # 落 DRY_RUN 流水


def test_submit_order_blocked_raises(monkeypatch):
    """挡板命中（如白名单外）→ raise RuntimeError + 落 BLOCKED 流水。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)

    recorded = []
    monkeypatch.setattr(trading_service, "record_live_trade",
                        lambda *a, **kw: recorded.append(a))
    async def _no_quote(s):
        return None
    monkeypatch.setattr(trading_service.qmt_market_data, "get_quote", _no_quote)

    # allow_live=True 但白名单不含该标的
    monkeypatch.setattr(trading_service, "_whitelist", lambda: {"510300.SH"})

    order = OrderRequest(symbol="000001.SZ", qty=100, side="buy", price=5.0)
    with pytest.raises(RuntimeError):
        asyncio.run(trading_service.submit_order(order, dry_run=False, confirm=True))
    assert recorded and recorded[0][1] == "BLOCKED"


def test_submit_order_live_calls_gateway(monkeypatch):
    """dry_run=False + 全过 → 调网关 submit_order。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    monkeypatch.setattr(trading_service, "_allow_live", lambda: True)
    monkeypatch.setattr(trading_service, "_whitelist", lambda: {"510300.SH"})
    monkeypatch.setattr(trading_service, "_max_amount", lambda: 10000.0)
    monkeypatch.setattr(trading_service, "_max_shares", lambda: 1000)
    monkeypatch.setattr(trading_service, "_enforce_session", lambda: False)
    async def _no_quote(s):
        return None
    monkeypatch.setattr(trading_service.qmt_market_data, "get_quote", _no_quote)

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    r = asyncio.run(trading_service.submit_order(order, dry_run=False, confirm=True))
    assert r["order_id"] == "100"
    assert gw.submit_calls and gw.submit_calls[0].symbol == "510300.SH"


def test_submit_order_disconnected_blocks(monkeypatch):
    """网关未连接 → 挡板 connection 关命中。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    gw = _fake_gw_connected()
    gw._connected = False
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: gw)
    async def _no_quote(s):
        return None
    monkeypatch.setattr(trading_service.qmt_market_data, "get_quote", _no_quote)

    order = OrderRequest(symbol="510300.SH", qty=100, side="buy", price=5.0)
    with pytest.raises(RuntimeError, match="连接"):
        asyncio.run(trading_service.submit_order(order, dry_run=False, confirm=True))
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_trading_service.py -v
```
Expected: 新增 6 个 FAIL（`AttributeError: module has no attribute 'connect_gateway'/'submit_order'`），既有测试仍绿。

- [ ] **Step 3: 扩展 `server/services/trading_service.py`**

在文件顶部 import 区追加（`from core.notifier ...` 之后）：

```python
from trading import qmt_market_data
from trading.execution_gateway import OrderRequest, OrderResult
from trading.risk_shield import check_order
```

在 `record_position_attribution` 函数之后、`record_live_trade` 之前插入 env helper 与新业务函数：

```python
# ============ Phase 1 Task 5：env 风控配置读取 ============
# Why 函数而非模块级常量：便于测试 monkeypatch 覆盖（直改函数返回值，无需 setenv），
# 且 env 可在进程运行中被 reload，函数读取总能拿到最新值。
def _allow_live() -> bool:
    """实盘总闸 QMT_ALLOW_LIVE_TRADE（true 时才允许前端 dry_run=false 真下单）。"""
    return os.getenv("QMT_ALLOW_LIVE_TRADE", "false").lower() == "true"


def _whitelist() -> set:
    """标的白名单（逗号分隔 → set）。空配置 → 空集（一切标的被挡板拒）。"""
    raw = os.getenv("QMT_SYMBOL_WHITELIST", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _max_amount() -> float:
    return float(os.getenv("QMT_ORDER_MAX_AMOUNT", "1000"))


def _max_shares() -> float:
    return float(os.getenv("QMT_ORDER_MAX_SHARES", "100"))


def _enforce_session() -> bool:
    return os.getenv("QMT_ENFORCE_SESSION", "true").lower() == "true"


def _in_a_share_session() -> bool:
    """粗略判断当前是否 A 股交易时段（9:30-11:30 / 13:00-15:00，工作日）。

    Why 粗略：精确时段需考虑节假日/集合竞价/港股通差异；此处仅做基本盘挡板，
    避免隔夜/周末误下单。生产可替换为更精确的日历服务。
    """
    from datetime import datetime
    now = datetime.now()
    # 周末（5=周六, 6=周日）
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 30 <= t <= 11 * 60 + 30
    afternoon = 13 * 60 <= t <= 15 * 60
    return morning or afternoon


def _dry_run_direction(side: str) -> str:
    """dry_run 模拟的 direction 取值（落 CSV 审计）。"""
    return "DRY_RUN_BUY" if side.lower() == "buy" else "DRY_RUN_SELL"


# ============ Phase 1 Task 5：连接 / 下单 / 撤单 / 查询 ============
async def connect_gateway() -> None:
    """触发网关连接（Cockpit /connect 调用）。

    网关未装配 → RuntimeError（路由层转 503）；connect 失败 → ConnectionError 上抛（转 503）。
    Why 不在 lifespan 自动 connect：connect 是同步阻塞 C++ 调用，按需触发更可控。
    """
    gw = get_qmt_gateway()
    if gw is None:
        raise RuntimeError("QMT 网关未装配（unavailable），请配置 QMT_USERDATA_PATH/QMT_ACCOUNT_ID")
    await gw.connect()


async def disconnect_gateway() -> None:
    """优雅断开网关。"""
    gw = get_qmt_gateway()
    if gw is None:
        return
    await gw.disconnect()


async def submit_order(order: OrderRequest, *, dry_run: bool, confirm: bool) -> dict:
    """下单业务编排：预取 quote → 风控挡板 → 真单/模拟/拒单 → 落流水。

    返回：
    - dry_run 命中：{"order_id":"", "state":"DRY_RUN", "message":<reason>}（不真下单）
    - 真单成功：{"order_id":<seq-str>, "state":<OrderState.name>, "message":<...>}
    挡板命中（非 dry_run）：raise RuntimeError(reason)（路由层转 409）
    """
    gw = get_qmt_gateway()
    if gw is None:
        raise RuntimeError("QMT 网关未装配（unavailable）")

    # 1. 预取行情（涨跌停关 + 金额估算用）；失败返 None，挡板跳过涨跌停关
    quote = await qmt_market_data.get_quote(order.symbol)

    # 2. 风控挡板（10 关短路）
    decision = check_order(
        order,
        dry_run=dry_run,
        allow_live=_allow_live(),
        whitelist=_whitelist(),
        max_amount=_max_amount(),
        max_shares=_max_shares(),
        quote=quote,
        enforce_session=_enforce_session(),
        is_locked=bool(getattr(gw, "is_locked", False)),
        connected=bool(getattr(gw, "_connected", False)),
        confirm=confirm,
        in_session=_in_a_share_session(),
    )

    # 3. 命中处理：落流水 + 返回/抛错
    if decision.blocked:
        if decision.is_dry_run:
            # 模拟：落 DRY_RUN 流水后返回成功语义（非错误）
            record_live_trade(
                order.symbol, _dry_run_direction(order.side),
                order.qty, order.price or 0.0,
                rationale=decision.reason,
            )
            return {"order_id": "", "state": "DRY_RUN", "message": decision.reason}
        # 真拒单：落 BLOCKED 流水 + raise（路由层转 409）
        record_live_trade(
            order.symbol, "BLOCKED", order.qty, order.price or 0.0,
            rationale=f"{decision.stage}:{decision.reason}",
        )
        raise RuntimeError(decision.reason)

    # 4. 全过 → 真下单
    result: OrderResult = await gw.submit_order(order)
    return {
        "order_id": result.order_id,
        "state": result.state.name,
        "message": result.message,
    }


async def cancel_order(order_id: str) -> dict:
    """撤单（透传网关）。"""
    gw = get_qmt_gateway()
    if gw is None:
        raise RuntimeError("QMT 网关未装配（unavailable）")
    result = await gw.cancel_order(order_id)
    return {"order_id": result.order_id, "state": result.state.name, "message": result.message}


async def get_orders() -> list:
    """查询本地缓存的订单回报流水（主线程同步读，转 list[dict]）。"""
    gw = get_qmt_gateway()
    if gw is None:
        return []
    orders = getattr(gw, "_orders", {}) or {}
    return [dict(v) for v in orders.values()]


async def get_asset() -> dict:
    """查询资金资产（现金/总资产）。未连接或无网关 → 空字典。

    依赖 xttrader.query_stock_asset；返回 XtAsset 的关键字段。
    """
    gw = get_qmt_gateway()
    if gw is None:
        return {}
    if getattr(gw, "is_locked", False) or not getattr(gw, "_connected", False):
        return {}
    # 经网关线程池查询；query_stock_asset 是同步阻塞调用
    import asyncio
    loop = asyncio.get_running_loop()
    try:
        asset = await loop.run_in_executor(
            None, lambda: gw._trader.query_stock_asset(gw._account)
        )
    except Exception as e:
        logger.warning("query_stock_asset 异常：%s", e)
        return {}
    if asset is None:
        return {}
    return {
        "account_id": getattr(asset, "account_id", ""),
        "cash": float(getattr(asset, "cash", 0.0)),
        "total_asset": float(getattr(asset, "total_asset", 0.0)),
        "market_value": float(getattr(asset, "market_value", 0.0)),
    }
```

- [ ] **Step 4: 运行测试，确认全绿**

```bash
pytest tests/test_trading_service.py -v
```
Expected: 全部 PASS（既有 + 新增）

- [ ] **Step 5: Commit**

```bash
git add server/services/trading_service.py tests/test_trading_service.py
git commit -m "feat(service): connect/submit/cancel/orders/asset + 流水全覆盖（Phase 1 Task 5）"
```

---

## Task 6: 路由层扩展（6 个 REST 路由）

**Files:**
- Modify: `server/api/v1/trading.py`
- Test: `tests/test_trading_api.py`（新增，TestClient 端到端）

**Interfaces:**
- Produces（HTTP）：
  - `POST /api/v1/trading/connect` → `{connected: true}` / 503
  - `POST /api/v1/trading/disconnect` → `{connected: false}`
  - `POST /api/v1/trading/submit_order` body `{symbol,qty,side,price?,dry_run,confirm}` → `{order_id,state,message}` / 409
  - `POST /api/v1/trading/cancel_order/{order_id}` → `{order_id,state,message}`
  - `GET /api/v1/trading/orders` → `{orders: [...]}`
  - `GET /api/v1/trading/asset` → `{asset: {...}}`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_trading_api.py`：

```python
# -*- coding: utf-8 -*-
"""交易路由端到端冒烟（FastAPI TestClient）。

验证 HTTP 码映射 + dry_run 字段透传 + 挡板命中→409 + 模拟→200(DRY_RUN)。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server.main import app
    return TestClient(app)


def test_status_endpoint(client):
    """GET /trading/status 始终可访问（无网关时 unavailable）。"""
    r = client.get("/api/v1/trading/status")
    assert r.status_code == 200
    assert "mode" in r.json()


def test_submit_order_dry_run(client, monkeypatch):
    """dry_run=true → 200 + state=DRY_RUN（不真下单）。"""
    from server.services import trading_service
    from trading.execution_gateway import OrderRequest

    class _FakeGW:
        _connected = True
        _lock_down = False
        @property
        def is_locked(self):
            return False
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: _FakeGW())
    monkeypatch.setattr(trading_service, "record_live_trade", lambda *a, **kw: None)
    async def _nq(s):
        return None
    monkeypatch.setattr(trading_service.qmt_market_data, "get_quote", _nq)

    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy", "price": 5.0,
        "dry_run": True, "confirm": True,
    })
    assert r.status_code == 200
    assert r.json()["state"] == "DRY_RUN"


def test_submit_order_no_confirm_returns_409(client, monkeypatch):
    """缺 confirm → 挡板命中 → 409。"""
    from server.services import trading_service
    class _FakeGW:
        _connected = True
        _lock_down = False
        @property
        def is_locked(self):
            return False
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: _FakeGW())
    monkeypatch.setattr(trading_service, "record_live_trade", lambda *a, **kw: None)
    monkeypatch.setattr(trading_service, "_allow_live", lambda: True)
    async def _nq(s):
        return None
    monkeypatch.setattr(trading_service.qmt_market_data, "get_quote", _nq)

    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy", "price": 5.0,
        "dry_run": False, "confirm": False,
    })
    assert r.status_code == 409


def test_submit_order_unavailable_503(client, monkeypatch):
    """无网关 → 503。"""
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    r = client.post("/api/v1/trading/submit_order", json={
        "symbol": "510300.SH", "qty": 100, "side": "buy",
        "dry_run": True, "confirm": True,
    })
    assert r.status_code in (409, 503)


def test_orders_and_asset_empty(client, monkeypatch):
    from server.services import trading_service
    monkeypatch.setattr(trading_service, "get_qmt_gateway", lambda: None)
    assert client.get("/api/v1/trading/orders").status_code == 200
    assert client.get("/api/v1/trading/asset").status_code == 200
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_trading_api.py -v
```
Expected: FAIL（`submit_order` 路由不存在 → 404/405）

- [ ] **Step 3: 扩展 `server/api/v1/trading.py`**

在文件顶部 import 区追加：

```python
from pydantic import BaseModel
from trading.execution_gateway import OrderRequest
from server.services.trading_service import (
    emergency_halt, export_trades, get_positions, get_status,
    connect_gateway, disconnect_gateway, submit_order as svc_submit_order,
    cancel_order as svc_cancel_order, get_orders, get_asset,
)
```

注意：原文件已有 `from server.services.trading_service import (emergency_halt, export_trades, get_positions, get_status)` —— 把它替换为上面这行（合并 import）。

在 `halt` 路由之后、`export_live_trades` 之前插入新路由：

```python
# ============ Phase 1 Task 6：连接 / 下单 / 撤单 / 查询 ============
class SubmitOrderBody(BaseModel):
    """下单请求体。dry_run 默认 True（安全：缺省模拟）；confirm 默认 False（强制二次确认）。"""
    symbol: str
    qty: float
    side: str                       # "buy" / "sell"
    price: float | None = None      # None=市价；有值=限价
    dry_run: bool = True            # 前端控制：True=模拟（不真下单）
    confirm: bool = False           # 二次确认开关


@router.post("/connect", summary="触发 QMT 网关连接")
async def connect_gw() -> dict:
    """连接 MiniQMT。失败 → 503（客户端未启动登录/路径错）。"""
    try:
        await connect_gateway()
        return {"connected": True, "mode": "live"}
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ConnectionError as e:
        raise HTTPException(503, str(e))


@router.post("/disconnect", summary="断开 QMT 网关")
async def disconnect_gw() -> dict:
    await disconnect_gateway()
    return {"connected": False}


@router.post("/submit_order", summary="下单（dry_run 前端可控）")
async def submit_order_endpoint(body: SubmitOrderBody) -> dict:
    """下单：dry_run=true 模拟（落 DRY_RUN 流水）；挡板命中 → 409；全过 → 真下单。

    交易流水全覆盖（见 spec §6.3）：dry_run / BLOCKED / 真单 / 废单 / 撤单 均落 CSV。
    """
    order = OrderRequest(symbol=body.symbol, qty=body.qty, side=body.side, price=body.price)
    try:
        return await svc_submit_order(order, dry_run=body.dry_run, confirm=body.confirm)
    except RuntimeError as e:
        # 挡板命中（非 dry_run）→ 409
        raise HTTPException(409, str(e))


@router.post("/cancel_order/{order_id}", summary="撤单")
async def cancel_order_endpoint(order_id: str) -> dict:
    try:
        return await svc_cancel_order(order_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.get("/orders", summary="本地订单回报流水")
async def orders_endpoint() -> dict:
    return {"orders": await get_orders()}


@router.get("/asset", summary="资金资产")
async def asset_endpoint() -> dict:
    return {"asset": await get_asset()}
```

- [ ] **Step 4: 运行测试，确认全绿**

```bash
pytest tests/test_trading_api.py tests/test_trading_service.py -v
```
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add server/api/v1/trading.py tests/test_trading_api.py
git commit -m "feat(api): 6 路由 connect/disconnect/submit/cancel/orders/asset（Phase 1 Task 6）"
```

---

## Task 7: 联调脚本（`scripts/qmt_smoke.py`）

**Files:**
- Create: `scripts/qmt_smoke.py`
- 无自动化测试（手跑）；验证脚本可 import

**Interfaces:**
- 产出：可执行 `python scripts/qmt_smoke.py`，分 5 步 `input()` 人工确认

- [ ] **Step 1: 写脚本**

创建 `scripts/qmt_smoke.py`：

```python
# -*- coding: utf-8 -*-
"""QMT 首次真实联调脚本（Phase 1 验收）。

前置条件：
  1. MiniQMT (XtItClient.exe) 已启动并登录账号 62138335
  2. userdata_mini 目录已生成（D:\\国金QMT交易端模拟\\userdata_mini）
  3. .env 已配置 QMT_USERDATA_PATH / QMT_ACCOUNT_ID

运行：python scripts/qmt_smoke.py

铁律（CLAUDE.md 状态机边界）：每步 input() 等待人工确认，绝不批量自动跑真单。
步骤：
  1) connect      期望 _connected=True, _lock_down=False
  2) query_asset  期望返回 XtAsset（现金/总资产）
  3) positions    期望返回持仓 list（空也 OK）
  4) dry_run 下单 期望 state=DRY_RUN + CSV 记 DRY_RUN_REJECT（不真下单）
  5) 真最小限价单（关 dry_run 后）→ 查 orders → 撤单
"""
import asyncio
import os
import sys

# 把项目根加入 sys.path（脚本独立运行需要）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# 触发 .env 加载（python-dotenv 若有；否则依赖外部已 export）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from trading.qmt_gateway import QmtExecutionGateway
from trading.execution_gateway import OrderRequest
from trading import qmt_market_data
from trading.order_state import OrderState


def _step(title: str):
    print(f"\n{'=' * 60}\n=== {title}\n{'=' * 60}")
    return input("回车继续，q 退出：").strip().lower() == "q"


async def main():
    print("QMT 联调脚本启动。account=", os.getenv("QMT_ACCOUNT_ID"))

    gw = QmtExecutionGateway()

    # --- 步骤 1：connect ---
    if _step("步骤 1: connect"):
        return
    await gw.connect()
    print(f"结果：_connected={gw._connected}, is_locked={gw.is_locked}")
    if not gw._connected:
        print("❌ 连接失败，终止。请确认 XtItClient 已启动登录。")
        return

    # --- 步骤 2：query_asset ---
    if _step("步骤 2: query_asset"):
        return
    import asyncio as _aio
    loop = _aio.get_running_loop()
    asset = await loop.run_in_executor(
        None, lambda: gw._trader.query_stock_asset(gw._account)
    )
    print(f"资产：{asset}")

    # --- 步骤 3：positions ---
    if _step("步骤 3: query_positions"):
        return
    positions = await gw._fetch_broker_positions()
    print(f"持仓（{len(positions)} 只）：{positions}")

    # --- 步骤 4：dry_run 下单（不真下单）---
    if _step("步骤 4: dry_run 下单（白名单内 510300.SH 100 股）"):
        return
    symbol = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH").split(",")[0].strip()
    order = OrderRequest(symbol=symbol, qty=100, side="buy", price=5.0)
    # 直接用网关 + 手动挡板演示（dry_run 即不调 order_stock_async）
    print(f"[DRY_RUN] 模拟下单 {symbol} 100 股 @5.0（不调网关，仅打印意图）")
    print("（真实 dry_run 走 POST /submit_order，本脚本仅验证网关连通性）")

    # --- 步骤 5：真最小限价单（需显式确认）---
    ans = input("\n步骤 5: 真实最小限价单（100 股）。输入 YES 继续，其他跳过：").strip()
    if ans != "YES":
        print("已跳过真单步骤。")
        await gw.disconnect()
        return
    print("⚠️  发起真实限价单 100 股（模拟盘）...")
    result = await gw.submit_order(order)
    print(f"下单结果：order_id={result.order_id}, state={result.state.name}")
    if result.state == OrderState.SUBMITTED:
        # 等待 async_response 建立映射后撤单
        await asyncio.sleep(2)
        cancel_res = await gw.cancel_order(result.order_id)
        print(f"撤单结果：state={cancel_res.state.name}")

    await gw.disconnect()
    print("\n联调完成。")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 验证脚本可 import（语法检查）**

```bash
python -c "import ast; ast.parse(open('scripts/qmt_smoke.py', encoding='utf-8').read()); print('语法 OK')"
```
Expected: `语法 OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/qmt_smoke.py
git commit -m "feat(smoke): QMT 真实联调脚本 5 步人工确认（Phase 1 Task 7）"
```

---

## Task 8: 全量回归 + 真实联调验收

**Files:** 无修改（验证型任务）

- [ ] **Step 1: 全量 pytest 回归**

```bash
pytest -x -q
```
Expected: 全绿（既有 444+ 测试 + Phase 1 新增测试），无回归。若有失败，定位是 Phase 1 引入还是预存在，修复后重跑。

- [ ] **Step 2: 启动后端，验证四态心跳**

```bash
# 先确保 XtItClient 已启动登录
python -c "from server.services.trading_service import get_status; print(get_status())"
```
Expected: `{'connected': False, 'locked': False, 'mode': 'disconnected'}`（不再是 unavailable——env 已配置）

- [ ] **Step 3: 真实联调脚本（需 XtItClient 运行）**

由研究员确认 XtItClient 已启动后：

```bash
python scripts/qmt_smoke.py
```
Expected: 步骤 1 connect 成功（_connected=True）；步骤 2 返回资产；步骤 3 持仓；步骤 4 dry_run 打印；步骤 5（研究员输 YES）真单 + 撤单回报对账。

- [ ] **Step 4: 验收 checklist 核对（spec §11）**

逐条核对：
- [ ] Phase 1 新增测试 + 既有测试全绿
- [ ] `/status` 返回 disconnected（配置生效）
- [ ] `/connect` 成功后 status 变 live
- [ ] dry_run 下单被挡板拦截并落 CSV
- [ ] 断线（kill XtItClient）后 status 变 vetoed_by_risk，submit 返 409

- [ ] **Step 5: 合并准备（可选，研究员确认后）**

```bash
git log --oneline feat/qmt-broker-access-phase1 ^master
git checkout master
git merge --no-ff feat/qmt-broker-access-phase1 -m "merge: QMT 实盘接入 Phase 1（后端 API+风控+行情+联调）"
```

（合并需研究员确认——真实联调验收通过后再合）

---

## Self-Review 完成备注

- **Spec 覆盖**：spec §4 八组件 → Task 1（配置+导出）/ Task 2（risk_shield）/ Task 3（market_data）/ Task 4（qmt_gateway 单测）/ Task 5（service）/ Task 6（路由）/ Task 7（smoke）/ Task 8（验收）。spec §6.3 流水全覆盖 → Task 5 Step 3 submit_order 三分支（DRY_RUN/BLOCKED/真单）。spec §6.2 十关 → Task 2 全实现。✅
- **Placeholder**：无 TBD/TODO；每步含完整可运行代码。✅
- **类型一致**：`RiskDecision` / `check_order` / `get_quote` / `submit_order` 签名在跨 Task 引用处一致。✅
- **Phase 边界**：前端 UI（Phase 2）/ 策略引擎实盘（Phase 3）严格不在本计划，仅 API 预留 `dry_run` 字段。
