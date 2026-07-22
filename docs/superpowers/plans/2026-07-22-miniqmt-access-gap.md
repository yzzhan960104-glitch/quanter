# miniQMT 接入补全 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对照迅投官方 xttrader/xtdata API 全集，补全现有 miniQMT 接入（`QmtExecutionGateway` + `qmt_market_data`）的 8 项缺失能力，覆盖账号安全盲区、二期熔断 equity 源、订单状态可靠性、行情批量优化。

**Architecture:** 纯增量——只在 `trading/qmt_gateway.py` / `trading/qmt_market_data.py` / `trading/engine.py` 加方法/回调，不动 connect/submit_order/cancel_order/seq↔real 映射主链路。复用现有三红线（C++ 同步调用投线程池 + wait_for 超时；回调 C++ 线程只解析+call_soon_threadsafe 投递主线程；状态字面量+_assert_status_contract 校验防版本漂移）。

**Tech Stack:** Python 3.10（`.venv310`）/ xtquant（xttrader+xtdata，延迟容错 import）/ pytest / 无新依赖。

**对应 Spec:** `docs/superpowers/specs/2026-07-22-miniqmt-access-gap-design.md`

## Global Constraints

- **语言**：所有代码注释 100% 中文（CLAUDE.md），What + Why。
- **Python 环境**：`.venv310/Scripts/python.exe`（xtquant 绑 3.10）；`pytest`。
- **影子红线**：不碰 `submit_order`/`cancel_order`/connect 主链路与 seq↔real 映射。
- **回调三红线**：`on_*` 回调在 xtquant C++ 线程，只做"解析 + `call_soon_threadsafe` 投递主线程"，零跨线程副作用（零直接写 `_orders`、零 await 协程、零钉钉直发）。
- **状态字面量 + 校验**：QMT 枚举值用模块级字面量（`_QMT_*`），`_assert_status_contract` 连接时校验防 xtquant 版本漂移。
- **TDD + frequent commits**：每 task 先红后绿，每 task 一个中文 conventional commit。
- **回归红线**：现有 `tests/trading/` 全绿不破（当前 88 passed，含实验系统新增）。
- **事实来源**：`dict.thinktrader.net/nativeApi/xttrader.html`（交易）+ `xtdata.html`（行情），不臆造 xtquant 字段。

---

## File Structure

**修改**：
- `trading/qmt_gateway.py`：+ `on_account_status`/`_on_account_status_change`（T1）、`query_asset`（T2）、`query_orders`/`query_trades`（T4）、`_main_push_available`+connect 兜底+`_sync_orders_if_stale`（T5）、`_assert_status_contract` 补全+`cancel_order` message（T6）、`_fetch_broker_positions` 扩展（T7）。
- `trading/qmt_market_data.py`：+ `get_quotes`（T3），`get_quote` 改委托。
- `trading/engine.py`：`stop_loss_monitor` 现价改批量 `get_quotes`（T3）；qty 读取迁移到 `{sym:{volume}}` 子键（T7）。

**新增测试**：
- `tests/trading/test_qmt_gateway.py`：T1/T2/T4/T5/T6/T7 全覆盖（若已存在则追加）。
- `tests/trading/test_qmt_market_data.py`：T3 批量 + get_quote 回归（若已存在则追加）。

**依赖链**：T5 ← T4（兜底用 query_orders）；T7 ← T3（迁移 stop_loss_monitor qty 读取，T3 先改现价批量）。其余独立。

---

## Task 1: `on_account_status` 回调（P0 · 账号停用感知）

**Files:**
- Modify: `trading/qmt_gateway.py`（加账号状态字面量 + `on_account_status` 回调 + `_on_account_status_change` 主线程处理）
- Test: `tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: 现有 `self._lock_down`（断线锁定标志）、`self._loop.call_soon_threadsafe`、`fire_and_forget`/`NotificationManager`（core.notifier 别名垫片，`_on_disconnect_fatal` 已用）
- Produces: `on_account_status(status)` 回调（XtQuantTraderCallback 协议方法）；`_on_account_status_change(status_int)` 主线程处理（8 态锁策略）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_qmt_gateway.py  -*- coding: utf-8 -*-
"""QmtExecutionGateway 补全单测（on_account_status / query_asset / query_orders / 兜底 / polish / 持仓扩展）。"""
import asyncio
import pytest

from trading import qmt_gateway
from trading.qmt_gateway import QmtExecutionGateway


class _FakeLoop:
    """模拟 asyncio loop：捕 call_soon_threadsafe 投递的回调，供断言。"""
    def __init__(self):
        self.calls = []
    def call_soon_threadsafe(self, cb, *args):
        self.calls.append((cb, args))
    def create_task(self, coro):
        # 防 fire_and_forget 真起线程；静默关闭协程
        coro.close()


class _FakeStatus:
    """模拟 XtAccountStatus。"""
    def __init__(self, status: int):
        self.account_id = "1000000365"
        self.account_type = 2
        self.status = status


def _make_gw_with_fake_loop(monkeypatch):
    """构造一个绕过 xtquant/连接的 QmtExecutionGateway + fake loop（专测回调处理）。"""
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:\\fake")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "1000000365")
    gw = QmtExecutionGateway()
    gw._loop = _FakeLoop()
    gw._lock_down = False  # 初始未锁
    return gw


def test_on_account_status_disables_sys_locks_and_alerts(monkeypatch):
    """DISABLEBYSYS(8) → 置 _lock_down=True + 告警通道被触发。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    alerted = []
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: alerted.append((s, lvl)))
    gw.on_account_status(_FakeStatus(8))  # DISABLEBYSYS
    # C++ 线程投递了主线程处理
    assert len(gw._loop.calls) == 1
    cb, args = gw._loop.calls[0]
    cb(*args)  # 主线程执行 _on_account_status_change
    assert gw._lock_down is True
    assert alerted == [(8, "ERROR")]


def test_on_account_status_ok_clears_lock(monkeypatch):
    """OK(0) → 清 _lock_down。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._lock_down = True
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: None)
    gw.on_account_status(_FakeStatus(0))
    cb, args = gw._loop.calls[0]
    cb(*args)
    assert gw._lock_down is False


def test_on_account_status_intermediate_states_only_log(monkeypatch):
    """CORRECTING(5)/WAITING_LOGIN(1)/INITING(4) 中间态不锁只 log。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: None)
    for s in (5, 1, 4):
        gw._loop.calls.clear()
        gw.on_account_status(_FakeStatus(s))
        cb, args = gw._loop.calls[0]
        cb(*args)
        assert gw._lock_down is False  # 中间态不锁


def test_on_account_status_closed_not_lock(monkeypatch):
    """CLOSED(6) 收盘后不锁。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    monkeypatch.setattr(qmt_gateway, "_alert_account_status", lambda g, s, lvl: None)
    gw.on_account_status(_FakeStatus(6))
    cb, args = gw._loop.calls[0]
    cb(*args)
    assert gw._lock_down is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py::test_on_account_status_disables_sys_locks_and_alerts -v`
Expected: FAIL（`on_account_status` / `_alert_account_status` 不存在）

- [ ] **Step 3: 实现 `on_account_status` + 字面量 + 主线程处理**

在 `trading/qmt_gateway.py` 的 `_QMT_ORDER_*` 字面量块之后，加账号状态字面量：

```python
# === QMT 账号状态整数契约（来源：xttrader.md「账号状态 account_status」表）=========
# Why 字面量不用 xtconstant.ACCOUNT_STATUS_*：同 order_status，防 xtquant 版本漂移 +
# 无 xtquant 环境仍可 import。_assert_status_contract 连接时校验（T6 补全）。
_QMT_ACC_INVALID = -1         # 无效           -> 锁 + 告警
_QMT_ACC_OK = 0               # 正常           -> 清锁
_QMT_ACC_WAITING_LOGIN = 1    # 连接中         -> log
_QMT_ACC_LOGINING = 2         # 登录中         -> log
_QMT_ACC_FAIL = 3             # 登录失败       -> 锁 + 告警
_QMT_ACC_INITING = 4          # 初始化中       -> log
_QMT_ACC_CORRECTING = 5       # 数据刷新校正中 -> log（校正完有新推送）
_QMT_ACC_CLOSED = 6           # 收盘后         -> 不锁（正常）
_QMT_ACC_ASSIS_FAIL = 7       # 穿透副链接断开 -> 锁 + 告警
_QMT_ACC_DISABLE_BYSYS = 8    # 系统停用（密码错误超限）-> 锁 + 告警
_QMT_ACC_DISABLE_BYUSER = 9   # 用户停用       -> 锁 + 告警

# 应触发熔断锁 + 告警的账号状态集合（账号级故障，on_disconnected 捕获不到）
_QMT_ACC_FATAL = frozenset({
    _QMT_ACC_INVALID, _QMT_ACC_FAIL, _QMT_ACC_ASSIS_FAIL,
    _QMT_ACC_DISABLE_BYSYS, _QMT_ACC_DISABLE_BYUSER,
})


def _alert_account_status(gw, status_int: int, level: str) -> None:
    """主线程：账号状态告警（fire_and_forget 跨线程安全，链路异常吞不影响主路径）。

    与 _on_disconnect_fatal 同通道，复用 core.notifier（infra.notifier 别名垫片）。
    """
    try:
        from core.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_risk_event(
            f"QMT 账号状态异常 status={status_int} account={gw._account_id}，网关已锁定", level))
    except Exception:
        pass
```

在 `QmtExecutionGateway` 类的回调区（`on_cancel_error` 之后、`on_order_stock_async_response` 之前或之后均可，建议紧邻 `on_disconnected`）加：

```python
    def on_account_status(self, status: Any) -> None:
        """账号状态变动推送（C++ 线程）：解析 status_int → 投递主线程。

        Why 独立于 on_disconnected：disconnected 是连接级（socket 断），account_status
        是账号级（账号被系统停用/登录失败/穿透副链断开，socket 可能仍在）。账号被
        DISABLEBYSYS（密码错误超限）时 on_disconnected 不一定触发，必须靠本回调感知，
        否则网关以为连着继续发废单。
        """
        try:
            status_int = int(getattr(status, "status", -1))
            self._loop.call_soon_threadsafe(self._on_account_status_change, status_int)
        except Exception:
            logger.exception("on_account_status 解析异常，已吞并以保护 C++ 线程")

    def _on_account_status_change(self, status_int: int) -> None:
        """主线程：按 8 态锁策略处理账号状态（由 on_account_status 投递）。

        - fatal 态（INVALID/FAIL/ASSIS_FAIL/DISABLEBYSYS/DISABLEBYUSER）：锁 + ERROR 告警
        - OK(0)：清锁（账号恢复正常）
        - CLOSED(6)：不锁（收盘后正常）
        - 中间态（WAITING_LOGIN/LOGINING/INITING/CORRECTING）：只 log，等后续推送
        """
        if status_int in _QMT_ACC_FATAL:
            self._lock_down = True
            logger.critical("【QMT 账号异常】status=%s account=%s 网关已锁定", status_int, self._account_id)
            _alert_account_status(self, status_int, "ERROR")
        elif status_int == _QMT_ACC_OK:
            self._lock_down = False
            logger.info("QMT 账号状态 OK account=%s，已清锁", self._account_id)
        else:
            # WAITING_LOGIN/LOGINING/INITING/CORRECTING/CLOSED 等非 fatal 态只 log
            logger.info("QMT 账号状态变动 status=%s account=%s（非 fatal，不锁）", status_int, self._account_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k on_account_status -v`
Expected: PASS（4 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/qmt_gateway.py tests/trading/test_qmt_gateway.py
git commit -m "feat(trading): on_account_status回调+8态锁策略（账号停用感知·补on_disconnected盲区）"
```

---

## Task 2: `query_asset`（P0 · 解锁二期熔断 equity 源）

**Files:**
- Modify: `trading/qmt_gateway.py`（加 `query_asset` 方法）
- Test: `tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: `self._trader.query_stock_asset(acc)`（xttrader.md「资产查询」，返 `XtAsset{account_id, cash, frozen_cash, market_value, total_asset}` 或 None）、现有 `self._loop.run_in_executor` + `asyncio.wait_for` + `_ORDER_TIMEOUT`
- Produces: `async query_asset() -> dict`（返 `{account_id, cash, total_asset, market_value}`，4 字段对齐一期 `trading_service.get_asset` 的 QMT 分支 + EMT `_fetch_asset` + 前端 `Asset` 类型；异常/None 返 `{}`）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/trading/test_qmt_gateway.py

class _FakeAsset:
    """模拟 XtAsset。"""
    def __init__(self):
        self.account_id = "1000000365"
        self.cash = 50000.0
        self.frozen_cash = 1000.0
        self.market_value = 200000.0
        self.total_asset = 250000.0


class _FakeTraderAsset:
    """模拟 self._trader，query_stock_asset 返 FakeAsset / None。"""
    def __init__(self, asset):
        self._asset = asset
    def query_stock_asset(self, account):
        return self._asset


def test_query_asset_normalizes_to_4fields(monkeypatch):
    """query_stock_asset 返 XtAsset → 标准化 {account_id, cash, total_asset, market_value}。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderAsset(_FakeAsset())
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_asset())
    assert result == {"account_id": "1000000365", "cash": 50000.0,
                      "total_asset": 250000.0, "market_value": 200000.0}
    # frozen_cash 不返回（前端不用，YAGNI）


def test_query_asset_none_returns_empty(monkeypatch):
    """query_stock_asset 返 None（查询失败/无资产）→ 返 {}（与一期 get_asset 缺失语义一致）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderAsset(None)
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_asset())
    assert result == {}


def test_query_asset_locked_returns_empty(monkeypatch):
    """网关锁定（断线保护）→ 返 {}（不脏读）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderAsset(_FakeAsset())
    gw._account = object()
    gw._connected = True
    gw._lock_down = True
    result = asyncio.run(gw.query_asset())
    assert result == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k query_asset -v`
Expected: FAIL（`query_asset` 不存在）

- [ ] **Step 3: 实现 `query_asset`**

在 `QmtExecutionGateway` 类的查询区（`_fetch_broker_positions` 之后）加：

```python
    async def query_asset(self) -> dict[str, Any]:
        """查询资金资产，返标准化 dict（投线程池调 query_stock_asset）。

        返回 {account_id, cash, total_asset, market_value}（4 字段，与一期
        trading_service.get_asset 的 QMT 分支 + EMT _fetch_asset + 前端 Asset 类型
        完全对齐；frozen_cash 不返回——前端不用，YAGNI）。
        异常/None/锁定 → 返 {}（与一期 get_asset 缺失语义一致，调用方按 {} 降级）。

        双消费者：一期 trading_service.get_asset（现 QMT 内联分支保持不动，增量不重构）
        + 二期 circuit_breaker.check_daily_loss_limit（total_asset 即 equity，解锁
        二期 live 必修 gap① post_close 熔断连线）。
        """
        if self._loop is None or self._trader is None or self._account is None:
            return {}
        if self._lock_down:
            logger.warning("QMT 网关已锁定，query_asset 返空（断线保护，防脏读）")
            return {}
        try:
            asset = await asyncio.wait_for(
                self._loop.run_in_executor(None, lambda: self._trader.query_stock_asset(self._account)),
                timeout=_ORDER_TIMEOUT,
            )
        except Exception as exc:
            logger.exception("QMT query_stock_asset 异常/超时：%s", exc)
            return {}
        if asset is None:
            return {}
        return {
            "account_id": str(getattr(asset, "account_id", "")),
            "cash": float(getattr(asset, "cash", 0.0)),
            "total_asset": float(getattr(asset, "total_asset", 0.0)),
            "market_value": float(getattr(asset, "market_value", 0.0)),
        }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k query_asset -v`
Expected: PASS（3 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/qmt_gateway.py tests/trading/test_qmt_gateway.py
git commit -m "feat(trading): query_asset资产查询（解锁二期熔断equity源·4字段对齐一期/前端）"
```

---

## Task 3: `get_quotes` 批量快照 + stop_loss_monitor 改消费（P0 · 行情优化）

**Files:**
- Modify: `trading/qmt_market_data.py`（加 `get_quotes`，`get_quote` 改委托）
- Modify: `trading/engine.py`（`stop_loss_monitor` 现价循环改批量 `get_quotes`）
- Test: `tests/trading/test_qmt_market_data.py`

**Interfaces:**
- Consumes: `xtdata.get_full_tick(stock_code_list)`（xtdata.md，原生支持 list，返 `{stock_code: {...tick}}`）
- Produces: `async get_quotes(symbols: list[str]) -> dict[str, Optional[Mapping]]`（缺失标的值 None）；`get_quote` 保持单只便利签名（内部委托 `get_quotes([symbol])`）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_qmt_market_data.py  -*- coding: utf-8 -*-
"""qmt_market_data 批量行情单测（get_quotes + get_quote 回归）。"""
import asyncio
import pytest

from trading import qmt_market_data


def test_get_quotes_batch_returns_dict(monkeypatch):
    """批量取多只：get_full_tick 返多只 dict → get_quotes 返 {symbol: tick}。"""
    fake_tick = {
        "600000.SH": {"last_price": 10.5, "high_limit": 11.5, "low_limit": 9.5},
        "000001.SZ": {"last_price": 15.2, "high_limit": 16.7, "low_limit": 13.7},
    }
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)
    captured = {}
    class _FakeXtdata:
        def get_full_tick(self, symbols):
            captured["symbols"] = symbols
            return fake_tick
    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())
    result = asyncio.run(qmt_market_data.get_quotes(["600000.SH", "000001.SZ"]))
    assert captured["symbols"] == ["600000.SH", "000001.SZ"]  # 原生 list 透传
    assert set(result.keys()) == {"600000.SH", "000001.SZ"}
    assert result["600000.SH"]["last_price"] == 10.5


def test_get_quotes_missing_symbol_is_none(monkeypatch):
    """get_full_tick 不含的标的 → 该 symbol 值 None（调用方按 None 降级）。"""
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)
    class _FakeXtdata:
        def get_full_tick(self, symbols):
            return {"600000.SH": {"last_price": 10.5}}  # 缺 000001.SZ
    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())
    result = asyncio.run(qmt_market_data.get_quotes(["600000.SH", "000001.SZ"]))
    assert result["600000.SH"]["last_price"] == 10.5
    assert result["000001.SZ"] is None


def test_get_quotes_xtdata_unavailable_returns_all_none(monkeypatch):
    """xtdata 不可用 → 所有标的值 None（不抛）。"""
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", False)
    result = asyncio.run(qmt_market_data.get_quotes(["600000.SH", "000001.SZ"]))
    assert result == {"600000.SH": None, "000001.SZ": None}


def test_get_quote_delegates_to_get_quotes(monkeypatch):
    """get_quote(symbol) 单只便利方法 → 内部走 get_quotes([symbol])[symbol]。"""
    monkeypatch.setattr(qmt_market_data, "_XTDATA_AVAILABLE", True)
    class _FakeXtdata:
        def get_full_tick(self, symbols):
            return {"600000.SH": {"last_price": 10.5}}
    monkeypatch.setattr(qmt_market_data, "xtdata", _FakeXtdata())
    result = asyncio.run(qmt_market_data.get_quote("600000.SH"))
    assert result == {"last_price": 10.5}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_market_data.py -v`
Expected: FAIL（`get_quotes` 不存在）

- [ ] **Step 3: 实现 `get_quotes` + `get_quote` 改委托**

在 `trading/qmt_market_data.py`，把现有 `get_quote` 替换为批量版 + 单只委托：

```python
async def get_quotes(symbols: list[str]) -> dict[str, Optional[Mapping[str, Any]]]:
    """批量取多标的 tick 快照（get_full_tick 原生支持 list）。

    返回 {symbol: tick_dict 或 None}：
    - 正常：symbol -> {last_price, high_limit, low_limit, open, pre_close, ...}
    - 缺失（get_full_tick 返 dict 不含该 symbol / 异常 / xtdata 不可用）：symbol -> None
    调用方按 None 降级（如 stop_loss_monitor 跳过该标的止损检查）。

    Why 批量：止损监控 N 只持仓，原 get_quote 单只循环 N 次 get_full_tick 调用 →
    批量 1 次（get_full_tick 原生支持 list），线程池调用 N→1。
    """
    if not symbols:
        return {}
    if not _XTDATA_AVAILABLE:
        logger.debug("xtdata 不可用，get_quotes 全返 None（降级模式）")
        return {s: None for s in symbols}
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: xtdata.get_full_tick(symbols))
    except Exception as exc:
        logger.warning("xtdata.get_full_tick 批量异常 symbols=%s: %s", symbols, exc)
        return {s: None for s in symbols}
    if not raw:
        return {s: None for s in symbols}
    # raw 可能只含部分 symbol；缺失的标 None
    return {s: (raw.get(s) if isinstance(raw, Mapping) else None) for s in symbols}


async def get_quote(symbol: str) -> Optional[Mapping[str, Any]]:
    """单标的快照（便利方法，内部委托 get_quotes 批量取 [symbol]）。

    保留原签名供 risk_shield 第9关涨跌停 / get_positions 市值富化等单只消费者无改动复用。
    返 None 场景同 get_quotes（xtdata 不可用 / 异常 / 不含该标的）。
    """
    return (await get_quotes([symbol])).get(symbol)
```

- [ ] **Step 4: 改 `engine.stop_loss_monitor` 用批量取价**

在 `trading/engine.py` 的 `stop_loss_monitor`，把"循环单只 get_quote"改为"批量 get_quotes 后循环读结果"。定位现有 `for sym, qty in positions.items():` 内取现价的代码块（当前 `quote = await qmt_market_data.get_quote(sym)`），替换为：

```python
        # 现价（C1 fix + T3 批量优化）：一次性批量取所有持仓现价，N 次 get_quote → 1 次 get_quotes
        quotes = await qmt_market_data.get_quotes(list(positions.keys()))
        n_checked = 0
        n_triggered = 0
        for sym, qty in positions.items():
            sp = stop_prices.get(sym) if stop_prices else None
            if sp is None:
                continue
            quote = quotes.get(sym)
            price = quote.get("last_price") if quote else None
            if price is None or price != price:  # None 或 NaN → 跳过（不发盲价单）
                logger.warning("stop_loss_monitor 现价缺失/异常，跳过 sym=%s", sym)
                continue
            n_checked += 1
            if price <= sp:
                # 跌破固定止损价 → 发卖出单（qty 来自持仓，T+1 只卖 can_use_volume>0 的可卖仓）
                # ...（现有 _submit 卖出单逻辑保持不变，price 用批量的）
```

**注意**：保留现有 `_submit(OrderRequest(symbol=sym, qty=qty, side="sell", price=price), ...)` 卖出单逻辑（qty 仍来自 `positions[sym]`，T7 会把 positions 结构从 `{sym: float}` 改为 `{sym: {volume}}`，届时再迁移 qty 读取；本 task 不碰 qty 读取，只改现价批量）。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_market_data.py tests/trading/test_engine.py -v`
Expected: PASS（批量 4 用例 + engine stop_loss 回归）

- [ ] **Step 6: 提交**

```bash
git add trading/qmt_market_data.py trading/engine.py tests/trading/test_qmt_market_data.py
git commit -m "feat(trading): get_quotes批量快照+stop_loss_monitor改批量取价（N只→1次调用）"
```

---

## Task 4: `query_orders` + `query_trades`（P1 · 主动查询）

**Files:**
- Modify: `trading/qmt_gateway.py`（加 `query_orders` / `query_trades`）
- Test: `tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: `self._trader.query_stock_orders(acc, cancelable_only=False)` / `query_stock_trades(acc)`（xttrader.md，返 `list[XtOrder]`/`list[XtTrade]` 或 None）
- Produces: `async query_orders(cancelable_only=False) -> list[dict]` / `async query_trades() -> list[dict]`（标准化字段；None/异常返 `[]`）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/trading/test_qmt_gateway.py

class _FakeOrder:
    def __init__(self):
        self.order_id = 100
        self.stock_code = "600000.SH"
        self.order_type = 23
        self.order_volume = 1000
        self.price = 10.5
        self.traded_volume = 1000
        self.traded_price = 10.5
        self.order_status = 56  # SUCCEEDED
        self.status_msg = ""
        self.order_remark = "test"


class _FakeTrade:
    def __init__(self):
        self.order_id = 100
        self.stock_code = "600000.SH"
        self.traded_volume = 1000
        self.traded_price = 10.5
        self.traded_amount = 10500.0
        self.traded_time = 20260722093000


class _FakeTraderOrders:
    """模拟 self._trader 的 order/trade 查询。"""
    def __init__(self, orders, trades):
        self._orders = orders
        self._trades = trades
    def query_stock_orders(self, account, cancelable_only=False):
        return self._orders
    def query_stock_trades(self, account):
        return self._trades


def test_query_orders_normalizes(monkeypatch):
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders([_FakeOrder()], None)
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_orders())
    assert len(result) == 1
    o = result[0]
    assert o["order_id"] == 100
    assert o["stock_code"] == "600000.SH"
    assert o["order_volume"] == 1000
    assert "state" in o  # _map_qmt_status(56) -> FILLED


def test_query_orders_none_returns_empty(monkeypatch):
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders(None, None)
    gw._account = object()
    gw._connected = True
    assert asyncio.run(gw.query_orders()) == []
    assert asyncio.run(gw.query_trades()) == []


def test_query_trades_normalizes(monkeypatch):
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderOrders(None, [_FakeTrade()])
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw.query_trades())
    assert result[0]["traded_volume"] == 1000
    assert result[0]["traded_amount"] == 10500.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k "query_orders or query_trades" -v`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 实现 `query_orders` + `query_trades`**

在 `QmtExecutionGateway` 查询区（`query_asset` 之后）加：

```python
    async def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        """查询当日委托（投线程池调 query_stock_orders），返标准化 dict 列表。

        用途：subscribe 失败兜底（T5 惰性同步）+ 二期盘后对账强化（不止持仓，还能对
        委托流水）。None/异常/锁定 → 返 []（调用方按空降级）。
        """
        if self._loop is None or self._trader is None or self._account is None:
            return []
        if self._lock_down:
            return []
        try:
            orders = await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, lambda: self._trader.query_stock_orders(self._account, cancelable_only)),
                timeout=_ORDER_TIMEOUT,
            )
        except Exception as exc:
            logger.exception("QMT query_stock_orders 异常/超时：%s", exc)
            return []
        if not orders:
            return []
        return [{
            "order_id": getattr(o, "order_id", 0),
            "stock_code": getattr(o, "stock_code", ""),
            "order_type": getattr(o, "order_type", 0),
            "order_volume": getattr(o, "order_volume", 0),
            "price": float(getattr(o, "price", 0.0) or 0.0),
            "traded_volume": getattr(o, "traded_volume", 0),
            "traded_price": float(getattr(o, "traded_price", 0.0) or 0.0),
            "order_status": getattr(o, "order_status", 255),
            "state": _map_qmt_status(getattr(o, "order_status", 255)).name,
            "status_msg": getattr(o, "status_msg", ""),
            "order_remark": getattr(o, "order_remark", ""),
        } for o in orders]

    async def query_trades(self) -> list[dict[str, Any]]:
        """查询当日成交（投线程池调 query_stock_trades），返标准化 dict 列表。

        None/异常/锁定 → 返 []。
        """
        if self._loop is None or self._trader is None or self._account is None:
            return []
        if self._lock_down:
            return []
        try:
            trades = await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, lambda: self._trader.query_stock_trades(self._account)),
                timeout=_ORDER_TIMEOUT,
            )
        except Exception as exc:
            logger.exception("QMT query_stock_trades 异常/超时：%s", exc)
            return []
        if not trades:
            return []
        return [{
            "order_id": getattr(t, "order_id", 0),
            "stock_code": getattr(t, "stock_code", ""),
            "traded_volume": getattr(t, "traded_volume", 0),
            "traded_price": float(getattr(t, "traded_price", 0.0) or 0.0),
            "traded_amount": float(getattr(t, "traded_amount", 0.0) or 0.0),
            "traded_time": getattr(t, "traded_time", 0),
        } for t in trades]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k "query_orders or query_trades" -v`
Expected: PASS（3 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/qmt_gateway.py tests/trading/test_qmt_gateway.py
git commit -m "feat(trading): query_orders/query_trades主动查询（subscribe兜底+对账强化）"
```

---

## Task 5: subscribe 失败惰性查询兜底（P1）

**Files:**
- Modify: `trading/qmt_gateway.py`（connect 标记 `_main_push_available` + 加 `_sync_orders_if_stale`）
- Test: `tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: T4 `query_orders()`；现有 connect 的 `sub_rc` 判定
- Produces: `self._main_push_available: bool`（subscribe 成败标志）；`async _sync_orders_if_stale() -> int`（主推不可用时主动 query_orders 同步 `self._orders`）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/trading/test_qmt_gateway.py

def test_connect_subscribe_fail_marks_main_push_unavailable(monkeypatch):
    """subscribe 返 -1 → _main_push_available=False（不再只 warning）。"""
    monkeypatch.setenv("QMT_USERDATA_PATH", "D:\\fake")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "1000000365")
    # mock xtquant 可用 + connect/subscribe 行为
    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)
    class _FakeTrader:
        def start(self): pass
        def connect(self): return 0
        def subscribe(self, account): return -1  # 失败
    monkeypatch.setattr(qmt_gateway, "XtQuantTrader", lambda path, sid: _FakeTrader())
    monkeypatch.setattr(qmt_gateway, "StockAccount", lambda acc: object())
    gw = QmtExecutionGateway()
    asyncio.run(gw.connect())
    assert gw._main_push_available is False
    assert gw._connected is True  # 连接成功，只是主推不可用


def test_sync_orders_if_stale_calls_query_orders_when_unavailable(monkeypatch):
    """_main_push_available=False → _sync_orders_if_stale 调 query_orders 补 _orders。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._main_push_available = False
    gw._account = object()
    gw._connected = True
    called = {"query_orders": 0}
    async def fake_query_orders(cancelable_only=False):
        called["query_orders"] += 1
        return [{"order_id": 100, "stock_code": "600000.SH", "state": "FILLED",
                 "order_status": 56, "order_volume": 1000, "traded_volume": 1000,
                 "traded_price": 10.5, "price": 10.5, "status_msg": "", "order_remark": "",
                 "order_type": 23}]
    gw.query_orders = fake_query_orders
    n = asyncio.run(gw._sync_orders_if_stale())
    assert called["query_orders"] == 1
    assert n == 1
    assert gw._orders.get("100") is not None  # 同步进 _orders


def test_sync_orders_if_stale_noop_when_push_available(monkeypatch):
    """_main_push_available=True → 不查（主推正常，无需兜底）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._main_push_available = True
    called = {"query_orders": 0}
    async def fake_query_orders(cancelable_only=False):
        called["query_orders"] += 1
        return []
    gw.query_orders = fake_query_orders
    asyncio.run(gw._sync_orders_if_stale())
    assert called["query_orders"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k "subscribe_fail or sync_orders" -v`
Expected: FAIL（`_main_push_available` / `_sync_orders_if_stale` 不存在）

- [ ] **Step 3: 实现 `_main_push_available` + connect 标记 + 惰性同步**

在 `QmtExecutionGateway.__init__` 的实例属性区（`self._connected = False` 附近）加：

```python
        # 主推可用性标志（T5）：subscribe 成功 True，失败 False（订单状态靠主动查询兜底）。
        # Why 单列：subscribe 失败时 connect 仍可能成功（socket 通），但拿不到 on_stock_order
        # 主推，订单状态盲区——此时靠 _sync_orders_if_stale 在触发点前主动 query_orders 补全。
        self._main_push_available: bool = True
```

在 `connect` 方法里，`sub_rc != 0` 的 warning 分支（现有"QMT subscribe 返回 %s"处）改为：

```python
        if sub_rc != 0:
            self._main_push_available = False  # 主推不可用，靠主动查询兜底
            logger.warning(
                "QMT subscribe 返回 %s（0=成功，-1=失败），委托/成交主推缺失，"
                "订单状态将退化为主动查询模式（_sync_orders_if_stale 触发点前补全）", sub_rc)
        else:
            self._main_push_available = True
```

在查询区（`query_trades` 之后）加惰性同步方法：

```python
    async def _sync_orders_if_stale(self) -> int:
        """主推不可用时惰性同步订单状态（subscribe 失败兜底）。

        策略：_main_push_available=True（主推正常）→ no-op；False → 调 query_orders
        主动拉当日委托，补全 self._orders。惰性同步（触发点前调，不引入后台定时轮询）。

        调用时机（由上层 engine 决定）：pre_open / stop_loss_monitor 等触发点前，
        若依赖 _orders 状态，先 await 本方法兜底。颈线法触发点低频，查询开销可接受。
        """
        if self._main_push_available:
            return 0
        try:
            orders = await self.query_orders()
        except Exception:
            logger.exception("_sync_orders_if_stale 查询失败，本轮跳过")
            return 0
        n = 0
        for o in orders:
            oid = str(o.get("order_id", ""))
            if oid:
                rec = dict(o)
                rec["_gc_ts"] = time.time()
                self._orders[oid] = rec
                n += 1
        if n:
            logger.info("惰性同步补全 %s 笔委托（主推不可用兜底）", n)
        return n
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k "subscribe_fail or sync_orders" -v`
Expected: PASS（3 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/qmt_gateway.py tests/trading/test_qmt_gateway.py
git commit -m "feat(trading): subscribe失败惰性查询兜底（_main_push_available+_sync_orders_if_stale）"
```

---

## Task 6: `_assert_status_contract` 补全 11 态 + cancel_order message（Minor）

**Files:**
- Modify: `trading/qmt_gateway.py`（`_assert_status_contract` 的 expected 补全；`cancel_order` 的 rc==0 message）
- Test: `tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: 现有 `_assert_status_contract`（:123）的 expected dict；`cancel_order` 的 rc==0 分支（:436）
- Produces: expected 补全到 11 态（+PARTSUCC_CANCEL=52/REPORTED_CANCEL=51/WAIT_REPORTING=49/UNKNOWN=255）；cancel message 明示非终态语义

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/trading/test_qmt_gateway.py

def test_assert_status_contract_validates_all_11_states(monkeypatch):
    """_assert_status_contract 应覆盖 11 个 order 状态字面量（不只 7 个）。

    构造一个 xtconstant 假对象，11 态值与模块字面量一致 → 校验通过（不抛）。
    再故意改一个（PART_CANCEL）→ 校验 fail-fast 抛 RuntimeError。
    """
    monkeypatch.setattr(qmt_gateway, "_XTQUANT_AVAILABLE", True)
    # 11 态假 xtconstant（与模块字面量一致）
    class _FakeXtconst:
        ORDER_JUNK = qmt_gateway._QMT_ORDER_JUNK
        ORDER_SUCCEEDED = qmt_gateway._QMT_ORDER_SUCCEEDED
        ORDER_PART_SUCC = qmt_gateway._QMT_ORDER_PART_SUCC
        ORDER_CANCELED = qmt_gateway._QMT_ORDER_CANCELED
        ORDER_PART_CANCEL = qmt_gateway._QMT_ORDER_PART_CANCEL
        ORDER_PARTSUCC_CANCEL = qmt_gateway._QMT_ORDER_PARTSUCC_CANCEL
        ORDER_REPORTED_CANCEL = qmt_gateway._QMT_ORDER_REPORTED_CANCEL
        ORDER_REPORTED = qmt_gateway._QMT_ORDER_REPORTED
        ORDER_WAIT_REPORTING = qmt_gateway._QMT_ORDER_WAIT_REPORTING
        ORDER_UNREPORTED = qmt_gateway._QMT_ORDER_UNREPORTED
        ORDER_UNKNOWN = qmt_gateway._QMT_ORDER_UNKNOWN
    monkeypatch.setattr(qmt_gateway, "xtconstant", _FakeXtconst)
    # 11 态全一致 → 不抛
    qmt_gateway._assert_status_contract()
    # 故意改 PART_CANCEL → fail-fast
    _FakeXtconst.ORDER_PART_CANCEL = 999  # 漂移
    with pytest.raises(RuntimeError, match="xtconstant 枚举契约漂移"):
        qmt_gateway._assert_status_contract()


def test_cancel_order_message_marks_non_terminal(monkeypatch):
    """cancel_order rc==0 的 message 应明示"最终态以 on_stock_order 推送为准"。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._connected = True
    gw._lock_down = False
    gw._account = object()
    class _FakeTrader:
        def cancel_order_stock(self, account, oid): return 0
    gw._trader = _FakeTrader()
    gw._seq_to_real = {"100": 999}
    result = asyncio.run(gw.cancel_order("100"))
    assert result.state.name == "CANCELLED"
    assert "on_stock_order" in result.message  # 明示最终态靠推送确认
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k "assert_status_contract or cancel_order_message" -v`
Expected: FAIL（11 态校验不全 / message 不含 on_stock_order）

- [ ] **Step 3: 补全 `_assert_status_contract` + 改 cancel message**

在 `_assert_status_contract` 的 `expected` dict 补全（加 4 个）：

```python
    expected = {
        "ORDER_JUNK": _QMT_ORDER_JUNK,
        "ORDER_SUCCEEDED": _QMT_ORDER_SUCCEEDED,
        "ORDER_PART_SUCC": _QMT_ORDER_PART_SUCC,
        "ORDER_CANCELED": _QMT_ORDER_CANCELED,
        "ORDER_PART_CANCEL": _QMT_ORDER_PART_CANCEL,
        "ORDER_PARTSUCC_CANCEL": _QMT_ORDER_PARTSUCC_CANCEL,      # 补全
        "ORDER_REPORTED_CANCEL": _QMT_ORDER_REPORTED_CANCEL,      # 补全
        "ORDER_REPORTED": _QMT_ORDER_REPORTED,
        "ORDER_WAIT_REPORTING": _QMT_ORDER_WAIT_REPORTING,         # 补全
        "ORDER_UNREPORTED": _QMT_ORDER_UNREPORTED,
        "ORDER_UNKNOWN": _QMT_ORDER_UNKNOWN,                       # 补全
    }
```

在 `cancel_order` 的 `rc == 0` 分支（现有 message"撤单指令已发出，等待回报确认"）改为：

```python
        if rc == 0:
            # 撤单指令已发出；rc 只表"指令发出成功"，最终态以 on_stock_order 推 CANCELLED 为准
            # （非终态：可能因订单已成交/已撤而撤单失败，由 on_cancel_error 推送）
            return OrderResult(order_id=order_id, state=OrderState.CANCELLED,
                               message="撤单指令已发出（非终态），最终态以 on_stock_order 推送 CANCELLED 为准")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k "assert_status_contract or cancel_order_message" -v`
Expected: PASS（2 用例）

- [ ] **Step 5: 提交**

```bash
git add trading/qmt_gateway.py tests/trading/test_qmt_gateway.py
git commit -m "fix(trading): _assert_status_contract补全11态+cancel_order message非终态语义"
```

---

## Task 7: `_fetch_broker_positions` 扩展字段 + 消费者迁移（Minor · 破坏性变更）

**Files:**
- Modify: `trading/qmt_gateway.py`（`_fetch_broker_positions` 返回结构 `{sym: float}` → `{sym: {volume, avg_price, open_price, yesterday_volume}}`）
- Modify: `trading/engine.py`（`stop_loss_monitor` qty 读取迁移到 `{sym:{volume}}["volume"]`）
- Modify: 其他消费者（核查 `BaseExecutionGateway.sync_positions` / 一期 `trading_service.get_positions`，按实际迁移）
- Test: `tests/trading/test_qmt_gateway.py`

**Interfaces:**
- Consumes: `XtPosition{volume, can_use_volume, open_price, avg_price, yesterday_volume}`（xttrader.md 持仓结构）
- Produces: `_fetch_broker_positions() -> {sym: {volume: float, avg_price: float, open_price: float, yesterday_volume: int}}`（破坏性变更，消费者必须迁移 qty 读取）

**⚠️ 破坏性变更**：返回结构从 `{sym: float}` 变为 `{sym: dict}`。所有读 `positions[sym]` 当 float 的消费者必须改 `positions[sym]["volume"]`。本 task 必须迁移全部消费者，否则运行时 TypeError。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/trading/test_qmt_gateway.py

class _FakePosition:
    def __init__(self, stock_code, volume, can_use, avg_price, open_price, yesterday):
        self.stock_code = stock_code
        self.volume = volume
        self.can_use_volume = can_use
        self.avg_price = avg_price
        self.open_price = open_price
        self.yesterday_volume = yesterday


class _FakeTraderPositions:
    def __init__(self, positions):
        self._positions = positions
    def query_stock_positions(self, account):
        return self._positions


def test_fetch_broker_positions_returns_extended_dict(monkeypatch):
    """返回 {sym: {volume, avg_price, open_price, yesterday_volume}}（扩展字段）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderPositions([
        _FakePosition("600000.SH", 1000, 1000, 10.0, 10.0, 1000),  # 可卖
        _FakePosition("000001.SZ", 500, 0, 15.0, 15.0, 0),          # T+1 冻结（过滤）
    ])
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw._fetch_broker_positions())
    # can_use_volume==0 过滤，只剩 600000.SH
    assert "000001.SZ" not in result
    pos = result["600000.SH"]
    assert pos["volume"] == 1000
    assert pos["avg_price"] == 10.0
    assert pos["open_price"] == 10.0
    assert pos["yesterday_volume"] == 1000


def test_fetch_broker_positions_volume_is_primary(monkeypatch):
    """volume 仍是主可用量（can_use_volume==0 过滤不变）。"""
    gw = _make_gw_with_fake_loop(monkeypatch)
    gw._trader = _FakeTraderPositions([
        _FakePosition("600000.SH", 2000, 2000, 10.0, 10.0, 2000),
    ])
    gw._account = object()
    gw._connected = True
    result = asyncio.run(gw._fetch_broker_positions())
    assert result["600000.SH"]["volume"] == 2000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k "fetch_broker_positions" -v`
Expected: FAIL（返回结构是 {sym: float}，不是 {sym: dict}）

- [ ] **Step 3: 扩展 `_fetch_broker_positions` 返回结构**

把 `_fetch_broker_positions` 的 `cleaned` 构造改为 dict-of-dict：

```python
        cleaned: dict[str, dict[str, Any]] = {}
        for p in positions:
            # 过滤可用为 0 的废弃持仓（已平仓残留 / T+1 冻结不可操作仓）
            if getattr(p, "can_use_volume", 0) == 0:
                continue
            cleaned[p.stock_code] = {
                "volume": float(getattr(p, "volume", 0)),              # 主可用量（可卖持仓）
                "avg_price": float(getattr(p, "avg_price", 0.0) or 0.0),  # 成本价（浮盈对账）
                "open_price": float(getattr(p, "open_price", 0.0) or 0.0),  # 开仓价
                "yesterday_volume": int(getattr(p, "yesterday_volume", 0) or 0),  # 昨夜股（T+1 判断强化）
            }
        logger.debug("QMT 对账拉取完成：有效持仓 %d 只", len(cleaned))
        return cleaned
```

同步更新 docstring（把"清洗为 {stock_code: volume}"改为"清洗为 {stock_code: {volume, avg_price, open_price, yesterday_volume}}"）。

- [ ] **Step 4: 迁移消费者（破环性修复）**

**核查并迁移所有读 `positions[sym]` 当 float 的消费者**。已知消费者：

1. `trading/engine.py::stop_loss_monitor`：`for sym, qty in positions.items():` → 改为 `for sym, pos in positions.items(): qty = pos["volume"]`。定位 T3 改过的现价批量循环块，把 qty 读取从 `positions[sym]`（float）改为 `pos["volume"]`（dict 子键）。

2. `trading/execution_gateway.py::BaseExecutionGateway.sync_positions` + `reconcile`：核查 `local_positions` 的类型契约。**注意**：`sync_positions(local_positions: Mapping[str, float])` 的契约是 `{sym: float}`——`_fetch_broker_positions` 返回 dict-of-dict 后，`reconcile(local, broker)` 的 broker 侧变成 dict-of-dict，与 local（{sym: float}）类型不匹配。**这是一个需设计判断的点**：要么 sync_positions 只取 broker 的 volume 子键做对账（`{sym: pos["volume"] for sym, pos in broker.items()}`），要么扩展 reconcile 契约。

   **本 task 的处理**：在 `sync_positions`（或 `_fetch_broker_positions` 的调用点）做一层扁平化——对账只关心 volume，所以在 sync_positions 模板方法里把 broker 扁平化为 `{sym: float}` 再传 reconcile（保持 reconcile 契约不变，最小改动）。具体：核查 `execution_gateway.py:174-188` 的 `sync_positions`，在 `broker_positions = await self._fetch_broker_positions()` 后加扁平化：
   ```python
   # _fetch_broker_positions 现返 {sym: {volume, ...}}；对账只关心 volume，扁平化为 {sym: float}
   if broker_positions and isinstance(next(iter(broker_positions.values()), None), dict):
       broker_positions = {s: p["volume"] for s, p in broker_positions.items()}
   ```
   （这样 reconcile 契约不变，扩展字段供其他消费者按需读 `_fetch_broker_positions()` 原始返回。）

3. 一期 `trading_service.get_positions`：核查是否直接读 `_fetch_broker_positions`。若读，按 dict-of-dict 取 volume。

**实现时先 `codegraph_explore _fetch_broker_positions` / `grep positions\[` 全量核查消费者**，逐一迁移。每迁移一个，跑相关测试确认不破。

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_gateway.py -k fetch_broker_positions -v`
Expected: PASS（2 用例）

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/ tests/test_execution_gateway.py tests/test_trading_service.py -q`
Expected: 全绿（消费者迁移后无回归）

- [ ] **Step 6: 提交**

```bash
git add trading/qmt_gateway.py trading/engine.py trading/execution_gateway.py tests/trading/test_qmt_gateway.py
git commit -m "feat(trading): _fetch_broker_positions扩展成本价/昨夜股字段+消费者迁移（浮盈对账增强）"
```

---

## Self-Review

**1. Spec 覆盖**：
- ① on_account_status → T1 ✓
- ② query_asset → T2 ✓
- ③ get_quotes 批量 → T3 ✓
- ④ query_orders/trades → T4 ✓
- ⑤ subscribe 兜底 → T5 ✓
- ⑥ _assert_status_contract 补全 → T6 ✓
- ⑦ cancel_order message → T6 ✓
- ⑧ _fetch_broker_positions 扩展 → T7 ✓
- spec §4.1 增量原则（不动主链路）→ 各 task 均只加方法/回调 ✓
- spec §7 不做清单（subscribe/历史/日历/详情/信用/约券）→ plan 无对应 task ✓

**2. Placeholder 扫描**：无 TBD/TODO；T7 Step 4 的消费者迁移有"先 codegraph/grep 核查全量消费者"的具体指令（非 placeholder，是必要的现有代码核查，因消费者分布需实现时确认）。

**3. 类型一致性**：
- `query_asset -> {account_id, cash, total_asset, market_value}`（T2 定义，spec §4.2② + 一期 get_asset 对齐）✓
- `get_quotes -> {symbol: Optional[Mapping]}`（T3 定义，T3 stop_loss_monitor 消费 `quotes.get(sym)["last_price"]`）✓
- `query_orders -> list[dict]`（T4 定义，T5 `_sync_orders_if_stale` 消费 `o["order_id"]`）✓
- `_fetch_broker_positions -> {sym: {volume, avg_price, open_price, yesterday_volume}}`（T7 定义，T7 sync_positions 扁平化消费 `p["volume"]`）✓
- `_main_push_available` / `_sync_orders_if_stale`（T5 定义）✓
- ACCOUNT_STATUS 8 态字面量（T1 定义）✓

**4. 依赖链**：T5←T4（query_orders）、T7←T3（stop_loss_monitor qty 读取，T3 先改现价批量，T7 再迁移 qty）。顺序 T1→T7 线性执行即可。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-22-miniqmt-access-gap.md`. Two execution options:

**1. Subagent-Driven（推荐）** — 每 task 派 fresh subagent + 任务间 review（与二期同模式，真金白银交易层严把关）。

**2. Inline Execution** — 当前 session 批量执行 + checkpoint。

> ⚠️ 这是交易网关层补全（影响实盘下单/账号安全/熔断 equity 源），执行时务必：①每 task review 严把关（尤其 T1 账号 8 态锁、T2 query_asset、T7 破坏性消费者迁移）；②T7 破坏性变更必须全量迁移消费者 + 跑回归；③现有影子模式不破（补全是能力增强，不改 live 路径）。

**Which approach?**
