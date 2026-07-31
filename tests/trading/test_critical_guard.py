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
