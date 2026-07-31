# -*- coding: utf-8 -*-
"""U2：_critical_guard wrapper + _halt 停调度原语单测。

覆盖：
- raise _CriticalHalt → _halted=True + sched.shutdown 被调 + _alert_critical 被调；
- _halted=True 时被装饰 job 入口即跳过（不执行函数体）；
- _halt 幂等（二次调不重复 shutdown）。
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from trading.engine import TradingEngine, _CriticalHalt, _critical_guard as _apply_guard


@pytest.mark.asyncio
async def test_critical_halt_triggers_halt_and_shutdown():
    """被装饰 method 内 raise _CriticalHalt → _halt 置 _halted + shutdown + alert。"""
    eng = TradingEngine()

    # 直接用真实的 _critical_guard 装饰一个会抛 _CriticalHalt 的协程函数
    @_apply_guard
    async def boom(self):
        raise _CriticalHalt("DB 写入失败 symbol=X")

    with patch("trading.engine._alert_critical") as ac, \
         patch.object(eng.sched, "shutdown") as sd:
        # _critical_guard 捕获 _CriticalHalt 后 _halt + 再 raise（让 apscheduler 顶层记日志）
        with pytest.raises(_CriticalHalt):
            await boom(eng)   # eng 作为 self 传入 wrapper
    assert eng._halted is True
    ac.assert_called_once()
    sd.assert_called_once_with(wait=False)


@pytest.mark.asyncio
async def test_halted_skips_decorated_job():
    """_halted=True → 被装饰 job 入口即 return，函数体不执行。"""
    eng = TradingEngine()
    eng._halted = True
    called = MagicMock()

    async def inner(self):
        called()

    decorated = _apply_guard(inner)
    await decorated(eng)
    called.assert_not_called()


@pytest.mark.asyncio
async def test_halt_is_idempotent():
    """二次 _halt 不重复 shutdown / 不重复 alert。"""
    eng = TradingEngine()
    with patch("trading.engine._alert_critical") as ac, \
         patch.object(eng.sched, "shutdown") as sd:
        eng._halt("第一次致命")
        eng._halt("第二次致命")
    assert eng._halted is True
    assert ac.call_count == 1   # 只告警一次
    assert sd.call_count == 1   # 只 shutdown 一次


# _apply_guard 见文件顶部 import（从 trading.engine 导入真实 _critical_guard 复用，不 reimplement）


# ============================================================================
# U6 e2e 事件链 gate（spec §9 验收标准 5 双层生效集成断言）
# ============================================================================
@pytest.mark.asyncio
async def test_e2e_pre_open_halt_then_stoploss_skipped(monkeypatch, tmp_path):
    """pre_open DB 失败 → _CriticalHalt → _halt 置 _halted → 后续 _stoploss job 入口即跳过。

    物理意图（spec §9 验收 5 · _halted + sched.shutdown 双层生效 · 集成层）：
        单测 test_critical_halt_triggers_halt_and_shutdown 已验「单 method raise → _halt」，
        本测补「跨 job 事件链」：pre_open 挂单循环 DB 写失败抛 _CriticalHalt → 被
        _critical_guard 捕获 _halt（_halted=True + sched.shutdown）→ 下一轮 _stoploss
        job 被 guard 顶 if _halted 兜底跳过，函数体不执行（防 30s 后重发=双倍卖）。
        覆盖 spec「raise 中断当前轮 + _halted 防下一轮」的 in-flight 全窗口。

    构造（最小集成 · 不真起 APScheduler）：
        1) 隔离 TRADE_PLAN_DIR + state_store DB + gw=None（pre_open 走到挂单循环）；
        2) patch pre_open 内的 insert_order 抛异常（DB 真相源失真 → L1）；
        3) patch _stoploss 的内部 stop_loss_monitor 为「若被调即标记 called=True」的哨兵——
           断言 _halted 后哨兵未被调（guard 在入口已 return）。
    """
    import asyncio  # noqa: F401  （asyncio.run 已在 pytest-asyncio event loop 上下文）
    from trading import engine, state_store, position_book
    from trading.compute.types import OrderRequest  # noqa: F401

    # ① 隔离：plan dir + state_store DB（init_store 建 account/order/trade_event 表）
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    _db = str(tmp_path / "e2e_state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", _db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", _db)
    position_book.init_db()
    state_store.init_store()

    eng = TradingEngine()
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", eng)
    # gate 绿（_pre_open_gate async 返 (True, "")）
    eng._pre_open_gate = AsyncMock(return_value=(True, ""))
    # gw=None 跳过撤昨日单 / 基线快照（pre_open 内 696/746 行 warning 路径）
    monkeypatch.setattr(engine, "get_gateway", lambda: None)

    # ② 落一份已确认 plan（1 只标的，让 _pre_open → pre_open 走到挂单循环）
    # ⚠️ 日期用「今日」（_pre_open 内部 datetime.now() 取 today，非写死 2099-01-02）
    from datetime import datetime
    from trading import trading_plan
    _today = datetime.now().strftime("%Y-%m-%d")
    trading_plan.save_plan(_today, [{
        "order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0},
        "formed_at": None,   # 不走 max_wait 窗口过滤
    }])
    trading_plan.confirm_plan(_today)

    # ③ patch calendar 放行交易日（否则 _pre_open 顶部 is_trading_day 守卫早返）
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    # ④ 哨兵：记录 stop_loss_monitor 是否被触达（_halted 后应 False）
    stoploss_called = {"v": False}

    async def _spy_stop_loss_monitor(**kw):
        stoploss_called["v"] = True
        return {"checked": 0}

    monkeypatch.setattr(engine, "stop_loss_monitor", _spy_stop_loss_monitor)

    # ⑤ 致命注入：pre_open 挂单循环内 insert_order(OPEN) 抛异常 → U3a L1 _CriticalHalt
    monkeypatch.setattr(state_store, "insert_order",
                        MagicMock(side_effect=RuntimeError("sqlite disk I/O error")))

    # ⑥ mock sched.shutdown 避免真关 APScheduler（_halt 会调 wait=False）
    monkeypatch.setattr(eng.sched, "shutdown", MagicMock())

    # ── 触发 _pre_open（被 _critical_guard 装饰）──
    # _critical_guard 捕获 _CriticalHalt 后 _halt + 再 raise（让 apscheduler 顶层记日志）
    with pytest.raises(_CriticalHalt):
        await eng._pre_open()

    # ── 断言 1：_halt 生效（_halted=True）──
    assert eng._halted is True, "pre_open DB 失败必须 _halt 置 _halted=True"

    # ── 断言 2：后续 _stoploss 被 guard 顶 if 跳过，stop_loss_monitor 未被触达 ──
    await eng._stoploss()
    assert stoploss_called["v"] is False, (
        "_halted=True 后 _stoploss 必须入口即跳过，不应触达 stop_loss_monitor（防双倍卖）")
