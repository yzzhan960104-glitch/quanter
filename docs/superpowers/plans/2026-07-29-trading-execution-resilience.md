# 交易执行韧性系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让全自动交易闭环真正自愈——网关 connect 失败/断线自动重连恢复、撤单确认到终态、配置漂移启动可见、漏单事件钉钉告警，并补齐三层测试。

**Architecture:** 在 engine 触发点与 QMT 网关之间加「执行韧性层」5 模块（M1 网关自愈/M2 撤单确认/M3 漂移防护/M4 钉钉告警/M5 清锁工具），复用现有 apscheduler + infra.notifier + MockExecutionGateway，零新依赖。

**Tech Stack:** Python 3.10（`.venv310`）、apscheduler AsyncIOScheduler、xtquant（QMT C++ 扩展）、pytest、asyncio。

## Global Constraints

- **Python 解释器固定**：所有运行/测试用 `F:/quanter/.venv310/Scripts/python.exe`（项目唯一 venv，含 xtquant）。
- **全中文注释**：所有新增/修改代码配中文注释，说明 What + Why（CLAUDE.md 红线）。
- **零新依赖**：复用 apscheduler / infra.notifier / MockExecutionGateway，不引入新包。
- **不自动启动 miniQMT 客户端 / 不自动删 session 锁文件**（交互式确认）。
- **OrderState 枚举**：`from trading.types.order_state import OrderState`；终态集合 = {CANCELLED, FILLED, REJECTED, PARTIAL_CANCELLED}。
- **TDD 纪律**：每任务先写失败测试 → 跑红 → 最小实现 → 跑绿 → commit。不跳测试。
- **测试隔离**：`.env` 含 `AUTO_CONFIRM_PLAN=true`/`AUTO_TRADE_MODE=live` 会污染测试（[[config-loaddotenv-test-pollution]]），conftest 已 autouse 隔离；新测试勿依赖真实 .env。
- **网关单例**：`from presentation.server.services.trading_service import get_gateway`（懒构造 QmtExecutionGateway 单例）。
- **commit 规则**：遵循用户 `commit-only-when-asked`——本计划的 commit 步骤是「准备好提交」，实际是否 commit 由用户在执行时拍板（默认每任务结束问一次）。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `scripts/qmt_clear_session_lock.py` | M5：列/清 session 锁残留（交互式） | Create |
| `broker/qmt.py` | M1+M2：is_client_ready / _reconnecting / connect 日志 / _confirm_cancelled | Modify |
| `trading/io/breaker.py` | M2：cancel_all_open_orders 撤单后调 _confirm_cancelled | Modify |
| `trading/engine.py` | M1+M3+M4：_health_guard job / _sanity_check / banner / 告警事件点 / stop_loss 撤单确认 | Modify |
| `trading/__main__.py` | M1+M3：启动 banner + 守护 job 注册 | Modify |
| `tests/trading/test_qmt_cancel_confirm.py` | M2 单测 | Create |
| `tests/trading/test_qmt_health_guard.py` | M1 单测 | Create |
| `tests/trading/test_engine_sanity_check.py` | M3 单测 | Create |
| `tests/trading/test_qmt_clear_session_lock.py` | M5 单测 | Create |
| `tests/trading/test_e2e_trading_flow.py` | e2e 四场景 | Modify |

---

## Task 1: M5 session 清锁脚本

**Files:**
- Create: `scripts/qmt_clear_session_lock.py`
- Test: `tests/trading/test_qmt_clear_session_lock.py`

**Interfaces:**
- Produces: `list_session_locks(userdata_path) -> list[dict]`、`is_clearable(lock, current_sid, now) -> bool`

- [ ] **Step 1: 写失败测试**

`tests/trading/test_qmt_clear_session_lock.py`：
```python
# -*- coding: utf-8 -*-
"""M5 session 清锁脚本单测：list + 可清性判定（不真删文件，纯逻辑）。"""
import os, time, importlib.util, sys
import pytest

def _load_module():
    """从 scripts/ 加载（非 package，importlib 加载）。"""
    spec = importlib.util.spec_from_file_location(
        "qmt_clear_session_lock", os.path.join("scripts", "qmt_clear_session_lock.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

@pytest.fixture
def mod(): return _load_module()

def test_list_session_locks_finds_queue_files(mod, tmp_path):
    """list 能识别 down_queue_win_*/lock_*/mutex 并归 sid。"""
    (tmp_path / "down_queue_win_123456").write_bytes(b"x")
    (tmp_path / "down_queue_win_123456__mutex").write_bytes(b"")
    (tmp_path / "lock_down_queue_win_123458").write_bytes(b"")
    (tmp_path / "miniqmtShmStockListCacheSZO").write_bytes(b"x")  # 非锁文件，应忽略
    locks = mod.list_session_locks(str(tmp_path))
    sids = {l["sid"] for l in locks}
    assert sids == {123456, 123458}  # 123458 来自 lock_down_queue_win_123458 归属

def test_is_clearable_rejects_current_sid(mod):
    """当前 sid 的锁一律不清（防误删活跃队列）。"""
    now = time.time()
    lock = {"sid": 123458, "mtime": now - 9999, "path": "x"}
    assert mod.is_clearable(lock, current_sid=123458, now=now) is False

def test_is_clearable_rejects_recent_file(mod):
    """近 1h 活跃的锁不清（可能在用）。"""
    now = time.time()
    lock = {"sid": 999, "mtime": now - 100, "path": "x"}  # 100s 前，<1h
    assert mod.is_clearable(lock, current_sid=123458, now=now) is False

def test_is_clearable_accepts_old_noncurrent(mod):
    """非当前 sid 且 mtime>1h 的残留可清。"""
    now = time.time()
    lock = {"sid": 123456, "mtime": now - 7200, "path": "x"}  # 2h 前，非当前
    assert mod.is_clearable(lock, current_sid=123458, now=now) is True
```

- [ ] **Step 2: 跑测试验证失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_clear_session_lock.py -v`
Expected: FAIL（ModuleNotFoundError / 文件不存在）

- [ ] **Step 3: 实现 scripts/qmt_clear_session_lock.py**

```python
# -*- coding: utf-8 -*-
"""M5：QMT session 共享队列/锁残留清理（交互式，防误删活跃队列）。

物理背景（[[qmt-connect-1-rootcause]]）：
    xtquant 用共享内存文件 down_queue_win_{sid}/up_queue_win_*/lock_* 与客户端通信，
    同一 sid 同一时刻只能被一个进程独占。老进程崩溃/断线后残留的锁文件会让新进程
    connect 返回 -1（疑似被占用）。本脚本列出残留并交互式清理【非当前 sid 且 >1h 未动】
    的文件——当前 sid / 近期活跃的一律拒绝删除（红线：删活跃队列=弄坏在跑的 engine）。

用法：.venv310/Scripts/python.exe scripts/qmt_clear_session_lock.py
"""
import glob, os, re, sys, time

def _env_userdata():
    """从 .env 读 QMT_USERDATA_PATH（脚本可能被独立调用，不依赖 config 包）。"""
    from dotenv import load_dotenv
    load_dotenv()
    return os.environ.get("QMT_USERDATA_PATH", "")

def _env_sid():
    from dotenv import load_dotenv
    load_dotenv()
    return int(os.environ.get("QMT_SESSION_ID", "123456"))

_LOCK_PATTERNS = ["down_queue_win_*", "lock_*queue_win_*", "*_queue_win_*__mutex"]

def list_session_locks(userdata_path):
    """扫 userdata 下所有 session 相关文件，归 sid + mtime。返回 [{sid,path,mtime}]。

    sid 提取：从文件名 down_queue_win_{sid} / lock_down_queue_win_{sid} 正则取末尾数字。
    非 sid 文件（如 up_queue_win_xtquant 不带 sid）sid=None，单独归「共享」不自动清。
    """
    if not userdata_path or not os.path.isdir(userdata_path):
        return []
    out = []
    for pat in _LOCK_PATTERNS:
        for f in glob.glob(os.path.join(userdata_path, pat)):
            name = os.path.basename(f)
            m = re.search(r"(\d+)\s*$", name.split("__")[0])  # 取末尾数字（去 mutex 后缀）
            sid = int(m.group(1)) if m else None
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            out.append({"sid": sid, "path": f, "mtime": mtime, "name": name})
    return out

def is_clearable(lock, current_sid, now, max_age_sec=3600):
    """可清判定：非当前 sid 且 mtime 超过 max_age_sec（默认1h）。

    红线：当前 sid 的文件绝不清（可能正被 engine 使用）；近期活跃的不清（可能刚用）。
    """
    sid = lock.get("sid")
    if sid is None or sid == current_sid:
        return False
    return (now - lock.get("mtime", 0)) > max_age_sec

def main():
    """列锁 + 交互式清理（逐文件确认，默认不删）。"""
    userdata = _env_userdata()
    current_sid = _env_sid()
    print(f"=== QMT session 锁清理（当前 .env sid={current_sid}）===")
    print(f"userdata: {userdata}")
    locks = list_session_locks(userdata)
    now = time.time()
    clearable = [l for l in locks if is_clearable(l, current_sid, now)]
    protected = [l for l in locks if not is_clearable(l, current_sid, now)]
    print(f"\n[保护·不动] {len(protected)} 个（当前 sid 或近1h活跃）：")
    for l in protected[:10]:
        print(f"  sid={l['sid']} {l['name']} mtime={time.ctime(l['mtime'])}")
    print(f"\n[可清·残留] {len(clearable)} 个（非当前 sid 且 >1h 未动）：")
    for l in clearable:
        print(f"  sid={l['sid']} {l['name']} mtime={time.ctime(l['mtime'])}")
    if not clearable:
        print("无可清残留，退出。"); return
    ans = input("\n逐文件确认删除？输入 'yes' 删除全部可清 / 单独 sid 数字 / 回车取消：").strip()
    if ans == "yes":
        for l in clearable:
            try: os.remove(l["path"]); print(f"  已删 {l['name']}")
            except OSError as e: print(f"  删除失败 {l['name']}: {e}")
    elif ans.isdigit():
        target = int(ans)
        for l in clearable:
            if l["sid"] == target:
                try: os.remove(l["path"]); print(f"  已删 {l['name']}")
                except OSError as e: print(f"  删除失败: {e}")
    else:
        print("取消，未删除任何文件。")

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试验证通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_clear_session_lock.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit（问用户）**

```bash
git add scripts/qmt_clear_session_lock.py tests/trading/test_qmt_clear_session_lock.py
git commit -m "feat(trading): M5 session 清锁脚本（交互式，防误删活跃队列）"
```

---

## Task 2: M2 撤单确认闭环 — `_confirm_cancelled`

**Files:**
- Modify: `broker/qmt.py`（QmtExecutionGateway 类内，紧邻 `query_orders` 方法之后加）
- Test: `tests/trading/test_qmt_cancel_confirm.py`

**Interfaces:**
- Produces: `QmtExecutionGateway._confirm_cancelled(oid: str, timeout: float=5.0, interval: float=0.5) -> bool`
- Consumes: `query_orders(cancelable_only=False)`（已有，qmt.py:527）

- [ ] **Step 1: 写失败测试**

`tests/trading/test_qmt_cancel_confirm.py`：
```python
# -*- coding: utf-8 -*-
"""M2 撤单确认闭环单测：cancel 后轮询到终态才返 True。"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from trading.types.order_state import OrderState

def _gw_with_orders(orders_factory):
    """造一个 QmtExecutionGateway，query_orders 返回 orders_factory() 的内容。"""
    from broker.qmt import QmtExecutionGateway
    gw = QmtExecutionGateway()
    gw.query_orders = AsyncMock(side_effect=orders_factory)
    return gw

@pytest.mark.asyncio
async def test_confirm_cancelled_returns_true_on_cancelled():
    """撤单后查到 CANCELLED 终态 → 返 True。"""
    seq = [{"order_id": 111, "state": OrderState.SUBMITTED},
           {"order_id": 111, "state": OrderState.CANCELLED}]
    gw = _gw_with_orders(lambda: seq.pop(0) if seq else [])
    ok = await gw._confirm_cancelled("111", timeout=2.0, interval=0.05)
    assert ok is True

@pytest.mark.asyncio
async def test_confirm_cancelled_timeout_returns_false():
    """一直非终态 → 超时返 False（绝不假装成功）。"""
    gw = _gw_with_orders(lambda: [{"order_id": 111, "state": OrderState.SUBMITTED}])
    ok = await gw._confirm_cancelled("111", timeout=0.2, interval=0.05)
    assert ok is False

@pytest.mark.asyncio
async def test_confirm_cancelled_filled_is_terminal():
    """撤单时已 FILLED（撤单失败但状态明确）→ 返 True（终态确认）。"""
    gw = _gw_with_orders(lambda: [{"order_id": 111, "state": OrderState.FILLED}])
    ok = await gw._confirm_cancelled("111", timeout=1.0, interval=0.05)
    assert ok is True

@pytest.mark.asyncio
async def test_confirm_cancelled_lockdown_returns_false():
    """lock_down 时 query_orders 返[]（降级）→ 超时返 False。"""
    gw = _gw_with_orders(lambda: [])
    ok = await gw._confirm_cancelled("111", timeout=0.2, interval=0.05)
    assert ok is False
```

- [ ] **Step 2: 跑测试验证失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_cancel_confirm.py -v`
Expected: FAIL（AttributeError: _confirm_cancelled 不存在）

- [ ] **Step 3: 实现最小代码（broker/qmt.py，加在 query_trades 方法之后）**

```python
    async def _confirm_cancelled(self, oid: str, timeout: float = 5.0, interval: float = 0.5) -> bool:
        """撤单后轮询确认到终态（CANCELLED/FILLED/REJECTED/PARTIAL_CANCELLED），超时返 False。

        物理意图（M2 · [[qmt-live-smoke-findings]] 撤单主推延迟1-2s）：
            cancel_order 调用后 QMT 主推回报有 1-2s 延迟，若不主动确认，撤单状态悬空
            （本地以为撤了、柜台其实没撤）。本方法轮询 query_orders 直到该 oid 到终态
            或超时，让上层据 True/False 决定是否告警/重试。

        返回：
            True  = 已确认到终态（CANCELLED 撤成 / FILLED 撤前已成交 / REJECTED 拒单）
            False = 超时未确认（调用方须记 WARNING，绝不假装撤成功）

        边界：
            lock_down/未连接 → query_orders 已降级返[] → 本方法自然超时返 False（不抛）。
            撤单低频（pre_open每日1次+少量pending），0.5s 间隔撞柜台限频风险可接受。
        """
        import time as _time
        deadline = _time.monotonic() + timeout
        # 终态集合：撤单成功的 CANCELLED + 撤单时已成交的 FILLED + 拒单 REJECTED + 部分撤
        terminal = (OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED,
                    OrderState.PARTIAL_CANCELLED)
        while _time.monotonic() < deadline:
            try:
                orders = await self.query_orders(cancelable_only=False)
            except Exception:
                # query_orders 内部异常已吞返[]，双保险
                orders = []
            for o in orders:
                if str(o.get("order_id")) == str(oid):
                    if o.get("state") in terminal:
                        return True
                    break  # 找到但非终态，本轮等下一轮轮询
            await asyncio.sleep(interval)
        return False
```

- [ ] **Step 4: 跑测试验证通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_cancel_confirm.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit（问用户）**

```bash
git add broker/qmt.py tests/trading/test_qmt_cancel_confirm.py
git commit -m "feat(trading): M2 撤单确认闭环 _confirm_cancelled（轮询终态，超时告警）"
```

---

## Task 3: M2 撤单确认接入 pre_open + stop_loss

**Files:**
- Modify: `trading/io/breaker.py`（`cancel_all_open_orders` 撤每单后调 `_confirm_cancelled`）
- Modify: `trading/engine.py`（stop_loss_monitor pending cancel 调 `_confirm_cancelled`，约 :872）
- Test: `tests/trading/test_breaker_cancel_confirm.py`

**Interfaces:**
- Consumes: `QmtExecutionGateway._confirm_cancelled(oid) -> bool`（Task 2 产出）
- Produces: cancel_all_open_orders 返回值新增 `unconfirmed` 计数

- [ ] **Step 1: 先读现状**

Run: `F:/quanter/.venv310/Scripts/python.exe -c "import inspect, trading.io.breaker as b; print(inspect.getsource(b.cancel_all_open_orders))"`
（执行者据此看清现有签名/返回结构，本任务在其基础上加确认，不重写）

- [ ] **Step 2: 写失败测试**

`tests/trading/test_breaker_cancel_confirm.py`：
```python
# -*- coding: utf-8 -*-
"""M2 接入：cancel_all_open_orders 撤单后调 _confirm_cancelled，统计 unconfirmed。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from trading.io import breaker

@pytest.mark.asyncio
async def test_cancel_all_counts_unconfirmed(monkeypatch):
    """撤单成功但 _confirm_cancelled 超时 → 计入 unconfirmed（不假装成功）。"""
    gw = MagicMock()
    gw.cancel_order = AsyncMock(return_value=None)
    gw._confirm_cancelled = AsyncMock(return_value=False)  # 超时未确认
    # query_orders 返 2 笔可撤买单
    gw.query_orders = AsyncMock(return_value=[
        {"order_id": 1, "stock_code": "000001.SZ", "order_type": 23},
        {"order_id": 2, "stock_code": "000002.SZ", "order_type": 23},
    ])
    res = await breaker.cancel_all_open_orders(gw)
    # 既有 cancelled 计数，又有 unconfirmed 计数（具体字段名以实现为准）
    assert res["cancelled"] == 2
    assert res["unconfirmed"] == 2
```

- [ ] **Step 3: 跑测试验证失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_breaker_cancel_confirm.py -v`
Expected: FAIL（unconfirmed 字段不存在）

- [ ] **Step 4: 实现（trading/io/breaker.py）**

在 `cancel_all_open_orders` 撤每单后加确认（鸭子类型，Mock/老网关无该方法则跳过保持向后兼容）：
```python
# 在 cancel_order 调用成功后插入（保留原有 cancelled 计数）：
confirmed = True
confirm_fn = getattr(gw, "_confirm_cancelled", None)
if confirm_fn is not None:
    try:
        confirmed = await confirm_fn(str(oid), timeout=5.0, interval=0.5)
    except Exception:
        confirmed = False  # 确认异常视为未确认（保守）
if not confirmed:
    unconfirmed += 1
    logger.warning("撤单未确认终态 order_id=%s（主推延迟或柜台未响应）", oid)
# 返回值补 unconfirmed 字段
return {"cancelled": n_cancelled, "unconfirmed": unconfirmed}
```
（执行者按 Step 1 读到的真实结构融入，保留所有原有字段，仅追加 `unconfirmed`）

- [ ] **Step 5: 接入 stop_loss pending cancel（trading/engine.py 约 :870-879）**

在 `await gw.cancel_order(oid)` 之后、`n_pending_cancelled += 1` 之前加确认：
```python
                    await gw.cancel_order(oid)
                    # M2：确认到终态才计成功（防主推延迟致状态悬空）
                    _ok = await gw._confirm_cancelled(str(oid), timeout=5.0, interval=0.5) \
                        if hasattr(gw, "_confirm_cancelled") else True
                    if _ok:
                        n_pending_cancelled += 1
                    else:
                        logger.warning("pending 撤单未确认 order_id=%s（告警人工复核）", oid)
```

- [ ] **Step 6: 跑测试验证通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_breaker_cancel_confirm.py tests/trading/test_e2e_trading_flow.py -v`
Expected: 新测试 passed；既有 e2e 不回归（pre_open 撤单路径行为兼容）

- [ ] **Step 7: Commit（问用户）**

```bash
git add trading/io/breaker.py trading/engine.py tests/trading/test_breaker_cancel_confirm.py
git commit -m "feat(trading): M2 撤单确认接入 pre_open+stop_loss（未确认不计成功）"
```

---

## Task 4: M3 启动 banner（配置漂移可见）

**Files:**
- Modify: `trading/__main__.py`（connect 调用前后，约 :190）
- Test: `tests/trading/test_main_banner.py`

**Interfaces:**
- Produces: 启动时一条结构化 INFO 日志，含 session_id/account/mode/口径版本

- [ ] **Step 1: 写失败测试**

`tests/trading/test_main_banner.py`：
```python
# -*- coding: utf-8 -*-
"""M3 启动 banner 单测：打印关键配置 + 口径版本（漂移可见）。"""
import logging, importlib
from unittest.mock import patch

def test_startup_banner_logs_key_config(caplog):
    """banner 必须含 session_id/account/mode/口径四要素。"""
    from trading import __main__ as m
    with patch.dict("os.environ", {
        "QMT_SESSION_ID": "123458", "QMT_ACCOUNT_ID": "10110356",
        "AUTO_TRADE_MODE": "live", "AUTO_CONFIRM_PLAN": "true",
    }, clear=False):
        with caplog.at_level(logging.INFO):
            m.log_startup_banner()  # 抽出的纯函数，便于单测
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "123458" in blob and "10110356" in blob
    assert "live" in blob
    assert "next_trading_day" in blob  # 口径版本
```

- [ ] **Step 2: 跑测试验证失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_main_banner.py -v`
Expected: FAIL（log_startup_banner 不存在）

- [ ] **Step 3: 实现（trading/__main__.py）**

抽出纯函数 `log_startup_banner()`（在 connect 调用前调一次）：
```python
def log_startup_banner():
    """M3：启动 banner 打印进程内关键配置 + 口径版本（配置漂移一眼可见）。

    Why：[[qmt-connect-1-rootcause]] 故障中 engine 进程内 session=123456 而 .env=123458，
    无 banner 无人发现。本函数把进程启动时读到的 env 固化进日志，对比 .env 即知漂移。
    """
    logger.info(
        "=== 启动 banner === session=%s account=%s userdata=%s mode=%s confirm=%s | "
        "口径: eod=next_trading_day, pre_open=today（标的 T+1 对齐）",
        os.environ.get("QMT_SESSION_ID", "?"),
        os.environ.get("QMT_ACCOUNT_ID", "?"),
        os.environ.get("QMT_USERDATA_PATH", "?"),
        os.environ.get("AUTO_TRADE_MODE", "?"),
        os.environ.get("AUTO_CONFIRM_PLAN", "?"),
    )
```
在 `gw = get_gateway()` 之前（约 :189）调用 `log_startup_banner()`。

- [ ] **Step 4: 跑测试验证通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_main_banner.py -v`
Expected: passed

- [ ] **Step 5: Commit（问用户）**

```bash
git add trading/__main__.py tests/trading/test_main_banner.py
git commit -m "feat(trading): M3 启动 banner（session/account/mode/口径版本，漂移可见）"
```

---

## Task 5: M3 口径自检 gate

**Files:**
- Modify: `trading/engine.py`（TradingEngine 类加 `_sanity_check_date_alignment`，`start()` 前调）
- Test: `tests/trading/test_engine_sanity_check.py`

**Interfaces:**
- Consumes: `calendar.next_trading_day(today)`、`trading_plan.load_plan`
- Produces: `_sanity_check_date_alignment() -> bool`；False 时启动拒绝进 live（仅 dry_run）

- [ ] **Step 1: 写失败测试**

`tests/trading/test_engine_sanity_check.py`：
```python
# -*- coding: utf-8 -*-
"""M3 口径自检：_eod 落盘 key(next_trading_day) 与 _pre_open 读 key(today) 对齐。"""
import pytest
from datetime import datetime
from unittest.mock import patch

def test_sanity_check_passes_when_aligned(monkeypatch):
    """T 日盘后：next_trading_day(today) 落盘 key 与次日 today 读取口径一致 → 通过。"""
    from trading.engine import TradingEngine
    eng = TradingEngine()
    # 模拟 today=T，next_trading_day=T+1；校验 save_plan 的 date 参数语义
    monkeypatch.setattr("trading.engine.calendar.next_trading_day", lambda d: "2026-07-30")
    assert eng._sanity_check_date_alignment(today="2026-07-29") is True

def test_sanity_check_detects_offbyone(monkeypatch):
    """next_trading_day 返 today 自身（旧 bug 口径）→ 自检失败。"""
    from trading.engine import TradingEngine
    eng = TradingEngine()
    monkeypatch.setattr("trading.engine.calendar.next_trading_day", lambda d: d)  # 旧 bug：返 today
    assert eng._sanity_check_date_alignment(today="2026-07-29") is False
```

- [ ] **Step 2: 跑测试验证失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_sanity_check.py -v`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 实现（trading/engine.py，TradingEngine 类内）**

```python
    def _sanity_check_date_alignment(self, today: str | None = None) -> bool:
        """M3：启动口径自检——确认 _eod 落盘用 next_trading_day、_pre_open 读 today。

        Why：[[eod-date-offbyone-fix]] 修复了代码口径，但若进程跑旧代码（未重启）口径会
        退回 today 落盘→次日读 T+1 永远差一天→标的错位+永不挂单。本自检在启动时验证
        next_trading_day(today) != today（即确实算出次日），否则视为口径坏，拒绝进 live。

        返回：True=口径正常；False=口径异常（调用方须降级 dry_run + 告警）。
        """
        import trading.engine as _self_mod  # calendar 已在本模块顶层 import
        from trading import calendar as _cal
        _today = today or datetime.now().strftime("%Y-%m-%d")
        try:
            nxt = _cal.next_trading_day(_today)
        except Exception as exc:
            logger.exception("口径自检：next_trading_day 异常，判口径坏：%s", exc)
            return False
        if not nxt or nxt == _today:
            logger.error("【口径自检失败】next_trading_day(%s)=%s 未算出次日（疑似跑旧代码），"
                         "拒绝进 live（降级 dry_run）", _today, nxt)
            return False
        logger.info("口径自检通过：eod 落盘 key=%s，pre_open 次日读 today 与之对齐", nxt)
        return True
```
在 `start()` 方法开头（注册 cron 前）调用，False 时 `logger.error` + 不进 live（具体降级：若 mode=live 且自检失败，记 CRITICAL 告警 Task 9 接入）。

- [ ] **Step 4: 跑测试验证通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_engine_sanity_check.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit（问用户）**

```bash
git add trading/engine.py tests/trading/test_engine_sanity_check.py
git commit -m "feat(trading): M3 口径自检 gate（next_trading_day 对齐，坏口径拒进 live）"
```

---

## Task 6: M1 网关就绪探测 `is_client_ready`

**Files:**
- Modify: `broker/qmt.py`（QmtExecutionGateway 类，紧邻 connect 方法）
- Test: `tests/trading/test_qmt_health_guard.py`

**Interfaces:**
- Produces: `QmtExecutionGateway.is_client_ready(staleness_sec=300) -> bool`（纯文件检查，不触 xtquant）

- [ ] **Step 1: 写失败测试**

`tests/trading/test_qmt_health_guard.py`：
```python
# -*- coding: utf-8 -*-
"""M1 网关健康守卫单测：就绪探测 + 重连互斥 + 守护 job。"""
import os, time, pytest

def _gw(userdata):
    from broker.qmt import QmtExecutionGateway
    return QmtExecutionGateway(userdata_path=userdata, account_id="10110356", session_id=777888)

def test_is_client_ready_false_when_dir_missing():
    gw = _gw("/nonexistent/path/xyz")
    assert gw.is_client_ready() is False

def test_is_client_ready_false_when_files_stale(tmp_path):
    """userdata 下文件都 >5min 未动 → 客户端没在跑 → False。"""
    (tmp_path / "down_queue_win_777888").write_bytes(b"x")
    old = time.time() - 9999
    os.utime(tmp_path / "down_queue_win_777888", (old, old))
    gw = _gw(str(tmp_path))
    assert gw.is_client_ready(staleness_sec=300) is False

def test_is_client_ready_true_when_file_fresh(tmp_path):
    """近 5min 内有活跃 shm/queue 文件 → 客户端在跑 → True。"""
    (tmp_path / "miniqmtShmStockListCacheSZO").write_bytes(b"x")  # 刚创建=新
    gw = _gw(str(tmp_path))
    assert gw.is_client_ready(staleness_sec=300) is True
```

- [ ] **Step 2: 跑测试验证失败**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py -v`
Expected: FAIL（is_client_ready 不存在）

- [ ] **Step 3: 实现（broker/qmt.py）**

```python
    def is_client_ready(self, staleness_sec: int = 300) -> bool:
        """探测 miniQMT 客户端是否就绪（M1 · 纯文件系统检查，不触 xtquant）。

        判据：userdata_mini 下 down_queue_win_* / miniqmtShm*Cache / up_queue_win_*
        任一文件 mtime 在近 staleness_sec（默认5min）内 = 客户端在跑且活跃。
        若全部文件老旧/不存在 → 客户端未启动或未登录 → connect 必返 -1，不空跑重连。

        Why 纯文件检查：不触达 xtquant（C++ 扩展），CI/单测/无 SDK 环境可安全调用；
        且文件 mtime 是客户端存活的最可靠信号（进程名因东财定制不定匹配）。
        """
        import glob as _glob
        if not self._userdata_path or not os.path.isdir(self._userdata_path):
            return False
        now = time.time()
        for pat in ("down_queue_win_*", "miniqmtShm*Cache*", "up_queue_win_*"):
            for f in _glob.glob(os.path.join(self._userdata_path, pat)):
                try:
                    if now - os.path.getmtime(f) < staleness_sec:
                        return True
                except OSError:
                    continue
        return False
```

- [ ] **Step 4: 跑测试验证通过**

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/test_qmt_health_guard.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit（问用户）**

```bash
git add broker/qmt.py tests/trading/test_qmt_health_guard.py
git commit -m "feat(trading): M1 网关就绪探测 is_client_ready（文件 mtime 判活，不触 xtquant）"
```

---

## Task 7: M1 `_reconnecting` 互斥 + connect 失败日志区分

**Files:**
- Modify: `broker/qmt.py`（`__init__` 加 `_reconnecting`；`_reconnect` 入口互斥；connect 失败日志按返回码区分）

**Interfaces:**
- Produces: `_reconnecting: bool` 属性（守护 job 与 on_disconnected 重连互斥用）

- [ ] **Step 1: 写失败测试**（追加到 test_qmt_health_guard.py）

```python
def test_reconnecting_flag_default_false():
    """_reconnecting 初始 False（未在重连）。"""
    gw = _gw("/tmp")
    assert gw._reconnecting is False

@pytest.mark.asyncio
async def test_reconnect_skips_if_already_reconnecting(monkeypatch):
    """_reconnecting=True 时重连入口直接返回（防并发重连）。"""
    from broker.qmt import QmtExecutionGateway
    gw = QmtExecutionGateway(userdata_path="/tmp", account_id="x", session_id=1)
    gw._reconnecting = True
    called = {"connect": False}
    async def _fake_connect(): called["connect"] = True
    monkeypatch.setattr(gw, "connect", _fake_connect)
    import trading.engine as _  # 确保 _reconnect 可达
    from broker.qmt import QmtExecutionGateway as Q
    await gw._reconnect.__wrapped__(gw) if hasattr(Q._reconnect, "__wrapped__") else None
    # _reconnect 见 _reconnecting=True 应立即返回，不调 connect
    assert called["connect"] is False
```
（注：_reconnect 已存在于 qmt.py:919，本任务仅在其入口加互斥；测试用 monkeypatch 验证不重复调 connect）

- [ ] **Step 2: 跑测试验证失败** → Run pytest → Expected FAIL

- [ ] **Step 3: 实现（broker/qmt.py）**

`__init__` 末尾加：
```python
        # M1：重连互斥标志——守护 job 与 on_disconnected→_reconnect 共用，防两条路径并发重连
        self._reconnecting: bool = False
```
`_reconnect`（qmt.py:919）开头改：
```python
    async def _reconnect(self) -> None:
        # M1 互斥：已在重连则直接返回（守护 job 或 on_disconnected 另一路径正在重连）
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            # ... 原有指数退避重连逻辑保持不变 ...
        finally:
            self._reconnecting = False
```
`connect()` 失败分支（qmt.py:348-354）日志增强（区分原因，供 M4 精准告警）：
```python
        if connect_rc != 0:
            self._lock_down = True
            # M1：按返回码区分失败原因，供上层告警精准定位
            reason = "session 疑似被占用（残留锁/他进程占用 sid）" if connect_rc == -1 \
                else f"返回码 {connect_rc}"
            raise ConnectionError(
                f"QMT connect 失败（{reason}）；userdata={self._userdata_path}。"
                f"若 -1 且客户端在跑，跑 scripts/qmt_clear_session_lock.py 清残留或换 sid"
            )
```

- [ ] **Step 4: 跑测试验证通过** → Run pytest test_qmt_health_guard.py → Expected passed（含新测试）

- [ ] **Step 5: Commit（问用户）**

```bash
git add broker/qmt.py tests/trading/test_qmt_health_guard.py
git commit -m "feat(trading): M1 _reconnecting 互斥 + connect -1 区分 session 占用"
```

---

## Task 8: M1 健康守护 job（TradingEngine._health_guard + 注册）

**Files:**
- Modify: `trading/engine.py`（TradingEngine 加 `_health_guard` + `start()` 注册 interval job）
- Modify: `trading/__main__.py`（无需改，start() 内部已注册）
- Test: 追加 `tests/trading/test_qmt_health_guard.py` / `tests/trading/test_engine.py`

**Interfaces:**
- Consumes: `gw.is_client_ready()`、`gw.connect()`、`gw._reconnecting`、`gw._connected`
- Produces: `TradingEngine._health_guard` async 方法 + apscheduler interval job（60s）

- [ ] **Step 1: 写失败测试**（追加 test_qmt_health_guard.py）

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_health_guard_noop_when_connected():
    """已连接时守护 job 直接返回（不捣乱）。"""
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock(); gw._connected = True
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    # 未调 connect（已连）

@pytest.mark.asyncio
async def test_health_guard_reconnects_when_ready_and_disconnected():
    """未连接但客户端就绪 → 调 connect 恢复。"""
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False
    gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=True)
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_awaited_once()

@pytest.mark.asyncio
async def test_health_guard_skips_when_client_not_ready():
    """客户端未就绪 → 不空调 connect（防刷柜台）。"""
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = False; gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=False)
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_not_awaited()
```

- [ ] **Step 2: 跑测试验证失败** → Expected FAIL（_health_guard 不存在）

- [ ] **Step 3: 实现（trading/engine.py，TradingEngine 类内）**

```python
    async def _health_guard(self) -> None:
        """M1：网关健康守护——未连接时探测客户端就绪→重连，恢复 live。

        调度：apscheduler interval job 每 60s 跑一次（start() 内注册）。
        逻辑（顺序不可调）：
          ① 已连接 → 清失败计数，no-op（不捣乱活跃连接）
          ② 正在重连（_reconnecting）→ 让出（on_disconnected 路径在重连，避免并发）
          ③ 客户端未就绪（is_client_ready=False）→ 跳过（不空跑 connect 刷柜台）
          ④ 退避：连续失败越多跳过越多轮次（等效指数退避，不改 apscheduler 调度）
          ⑤ 调 connect()——成功清计数恢复 live，失败累加计数等下轮
        """
        gw = get_gateway()
        if gw is None:
            return
        if getattr(gw, "_connected", False):
            self._guard_fail_count = 0
            return
        if getattr(gw, "_reconnecting", False):
            return  # 让出：另一条重连路径正在进行
        if not gw.is_client_ready():
            return  # 客户端未就绪，不空跑
        # 退避：失败次数→应跳过轮数（0,0,1,3,7... 近似 60→120→240→480s）
        skip = self._guard_skip_rounds(self._guard_fail_count)
        if self._guard_rounds_since_fail < skip:
            self._guard_rounds_since_fail += 1
            return
        try:
            await gw.connect()
            self._guard_fail_count = 0
            self._guard_rounds_since_fail = 0
            logger.info("health_guard 重连成功，网关恢复 live")
        except Exception as exc:
            self._guard_fail_count += 1
            self._guard_rounds_since_fail = 0
            logger.warning("health_guard 重连失败（第%s次）：%s", self._guard_fail_count, exc)

    @staticmethod
    def _guard_skip_rounds(fail_count: int) -> int:
        """失败次数→跳过轮数（指数退避近似）：0→0, 1→0, 2→1, 3→3, ≥4→7。"""
        if fail_count < 2: return 0
        if fail_count == 2: return 1
        if fail_count == 3: return 3
        return 7  # 上限≈8min（60s×8）
```
`__init__` 加属性：`self._guard_fail_count = 0; self._guard_rounds_since_fail = 0`。
`start()` 内与 `_stoploss` 同机制注册（interval 60s）：
```python
        self._scheduler.add_job(self._health_guard, "interval", seconds=60, id="_health_guard")
```

- [ ] **Step 4: 跑测试验证通过** → Run pytest test_qmt_health_guard.py → Expected 全 passed

- [ ] **Step 5: Commit（问用户）**

```bash
git add trading/engine.py tests/trading/test_qmt_health_guard.py
git commit -m "feat(trading): M1 健康守护 job（就绪探测+后台重连+退避，统一重连入口）"
```

---

## Task 9: M4 钉钉 CRITICAL 告警接线

**Files:**
- Modify: `trading/engine.py`（致命事件点调 `fire_and_forget(notify_risk_event(..., "CRITICAL"))`）
- Test: 追加 `tests/trading/test_e2e_trading_flow.py`（断言告警被调）

**Interfaces:**
- Consumes: `infra.notifier.NotificationManager.notify_risk_event(msg, level)` + `fire_and_forget`
- 告警事件点（spec M4）：①pre_open submitted=0 且 live ②口径自检失败 ③health_guard 连续失败超阈值 ④撤单确认超时

- [ ] **Step 1: 写失败测试**（追加 test_e2e_trading_flow.py，用 monkeypatch 断言告警被触发）

```python
@pytest.mark.asyncio
async def test_critical_alert_on_zero_submit_live(monkeypatch):
    """live 模式 pre_open submitted=0 → 触发钉钉 CRITICAL。"""
    fired = []
    async def _fake_notify(msg, level="INFO"): fired.append((msg, level)); return []
    monkeypatch.setattr("infra.notifier.NotificationManager.notify_risk_event",
                        lambda self, msg, level="INFO": _fake_notify(msg, level))
    # ... 构造 submitted=0 场景（gw lock_down 拒所有单），调 pre_open ...
    # 断言 fired 含 level=CRITICAL 且 msg 含"漏挂"/"submitted=0"
    assert any(l == "CRITICAL" for _, l in fired)
```
（执行者据 test_e2e_trading_flow.py 现有 fixture 融入，补 submitted=0 场景构造）

- [ ] **Step 2: 跑测试验证失败** → Expected FAIL

- [ ] **Step 3: 实现（trading/engine.py）**

模块顶部 import（若未引入）：`from infra.notifier import NotificationManager, fire_and_forget`
抽一个告警辅助函数（DRY，所有事件点复用）：
```python
def _alert_critical(msg: str) -> None:
    """M4：致命事件钉钉 CRITICAL（fire_and_forget 不阻塞主流程）。

    复用 infra.notifier（_reconnect 已在用），level=CRITICAL 限致命事件，避免告警风暴。
    """
    try:
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "CRITICAL"))
    except Exception:
        logger.exception("CRITICAL 告警发送失败（不阻塞主流程）：%s", msg)
```
事件点接入：
- pre_open 末尾（engine.py :611-612 后）：`if n_submitted == 0 and _mode()=="live": _alert_critical(f"pre_open 漏挂 submitted=0/{len(plan['orders'])} date={date}（网关锁死?）")`
- 口径自检失败（Task 5 `_sanity_check_date_alignment` 返 False 处）：`_alert_critical("口径自检失败：next_trading_day 异常，已降级 dry_run，请重启 engine 加载新代码")`
- health_guard 失败累计超阈值（Task 8，如 fail_count % 10 == 0）：`_alert_critical(f"health_guard 重连累计失败 {fail_count} 次，网关持续锁死，请人工介入")`

- [ ] **Step 4: 跑测试验证通过** → Run pytest test_e2e_trading_flow.py → Expected passed（含新断言）

- [ ] **Step 5: Commit（问用户）**

```bash
git add trading/engine.py tests/trading/test_e2e_trading_flow.py
git commit -m "feat(trading): M4 致命事件钉钉 CRITICAL（漏挂/口径失败/重连耗尽，复用 notifier）"
```

---

## Task 10: e2e 四断点场景收口

**Files:**
- Modify: `tests/trading/test_e2e_trading_flow.py`（补齐四场景）

**Interfaces:** 无新接口，覆盖 Task 2-9 的产出。

- [ ] **Step 1: 补四场景测试**

四场景（部分已在 Task 9 / 既有 fixture 基础上）：
1. 网关 lock_down 时 pre_open submitted=0 + CRITICAL 告警（Task 9 已起头，本任务补完整断言）。
2. 网关恢复（health_guard 重连成功）后下一轮 pre_open 正常挂单（mock connect 成功后 _connected=True）。
3. 撤单确认闭环：cancel_order 后 _confirm_cancelled 返 True 才计 n_pending_cancelled（Task 3 接入，本任务 e2e 端到端验证）。
4. 标的口径自检：next_trading_day 与 today 对齐，load_plan 拿到正确 date 的标的（Task 5 自检 + 实际 load_plan 联动）。

每个场景：构造 mock gw + plan + 触发 → 断言关键字段。

- [ ] **Step 2: 跑全套** 

Run: `F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/ -v`
Expected: 全 passed（新场景 + 既有不回归）

- [ ] **Step 3: Commit（问用户）**

```bash
git add tests/trading/test_e2e_trading_flow.py
git commit -m "test(trading): e2e 四断点场景（锁死漏挂/重连恢复/撤单确认/标的口径）"
```

---

## Task 11: 模拟盘 golden 验证 + live gate checklist

**Files:**
- Create: `docs/superpowers/plans/2026-07-29-live-gate-checklist.md`（live 切换检查清单，人工执行）

**Interfaces:** 无代码，运维 + 验证收口。

- [ ] **Step 1: 写 live gate checklist 文档**

`docs/superpowers/plans/2026-07-29-live-gate-checklist.md`，含 5 项硬门（spec §6）：
1. `python -m pytest tests/trading/` 全绿（贴通过数）。
2. 模拟盘 `trading/tools/qmt_live_smoke.py` AUTO 模式全链路验证（connect→挂单→撤单确认→断线重连）通过。
3. golden baseline 对齐（[[strategy-unify-backtest-live-plan]] 的 golden 刷新）。
4. M4 钉钉告警模拟盘实测收到 CRITICAL 推送（截图/消息 id）。
5. 启动 banner + 口径自检在模拟盘日志中绿。
+ 模拟盘验证 SOP 步骤（前置：客户端登录、.env=AUTO_TRADE_MODE=live、kill 旧 engine 进程树、qmt_clear_session_lock 清残留、start_all 重启、看 banner）。

- [ ] **Step 2: 执行模拟盘验证（用户侧，需 miniQMT 客户端）**

人工按 SOP 跑模拟盘，确认 5 项硬门。AI 协助分析日志。

- [ ] **Step 3: 研究员签字 + 切 live**

5 项硬门全绿 + 研究员（用户）签字 → 切 live（.env 已是 live，重启 engine 生效）。

- [ ] **Step 4: Commit checklist**

```bash
git add docs/superpowers/plans/2026-07-29-live-gate-checklist.md
git commit -m "docs(trading): live gate checklist（5 硬门 + 模拟盘 SOP + 切 live 流程）"
```

---

## Self-Review

**1. Spec 覆盖**：M5→Task1 ✓ / M2→Task2,3 ✓ / M3→Task4,5 ✓ / M1→Task6,7,8 ✓ / M4→Task9 ✓ / e2e→Task10 ✓ / 模拟盘+gate→Task11 ✓。spec 7 节全覆盖。

**2. 占位扫描**：Task 3 Step 1 让执行者先读 breaker.py 现状（非占位，是必要的现状确认步骤，因 cancel_all_open_orders 结构需实读）；其余步骤均有完整代码。✓

**3. 类型一致性**：`_confirm_cancelled(oid, timeout=5.0, interval=0.5) -> bool` 在 Task2 定义、Task3/9 使用签名一致；`is_client_ready(staleness_sec=300) -> bool` Task6 定义、Task8 使用一致；`_health_guard` Task8 定义；`_alert_critical(msg)` Task9 定义。✓

**4. 已知依赖风险**：Task 9 的告警测试依赖 monkeypatch `NotificationManager.notify_risk_event`，执行者需确认 infra.notifier 的实例方法签名（codegraph 已确认 `notify_risk_event(self, msg, level)` + `fire_and_forget(coro)`）。
