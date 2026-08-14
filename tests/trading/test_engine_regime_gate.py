# -*- coding: utf-8 -*-
"""A1 engine 双前置测试：regime BEAR/UNKNOWN → eod 停产 + pre_open 拒单（TDD）。

物理意图：颈线法 2022 熊市折外 calmar=-0.62（wf 四折实证），空头环境假突破多——
A1 闸让 live 在 BEAR/UNKNOWN（fail-closed）环境停新单。本文件锁 engine 侧
`_regime_gate` 三态与 `_pre_open_gate` ④ 段；regime 判定本体见 test_regime.py。
"""
import pytest
from unittest.mock import patch

from trading.engine import TradingEngine
from trading.compute.regime import RegimeState

# 模块级 async 标记（pytest.ini asyncio_mode=strict；同 tests/trading/test_breaker_cancel_confirm.py 先例）
pytestmark = pytest.mark.asyncio


def _rs(state, reason="测试", asof="2026-08-14"):
    """RegimeState 快速构造。"""
    return RegimeState(state=state, reason=reason, asof=asof)


@pytest.fixture(autouse=True)
def _fake_lake(monkeypatch):
    """装配层桩：_regime_gate 经 data_ctx.load_regime_frames 读湖（455MB）——
    gate 测试焦点是三态语义，桩掉读湖（classify 本身在各测试内 patch）。"""
    monkeypatch.setattr("trading.data_ctx.load_regime_frames",
                        lambda: (None, None))


@pytest.fixture
def eng():
    """零 I/O 构造（__init__ 只装配 scheduler，不 connect 不起 cron）。"""
    return TradingEngine()


# ============ _regime_gate 本体三态 ============
class TestRegimeGate:
    async def test_bull_passes(self, eng):
        """BULL → (True, "")放行。"""
        with patch("trading.compute.regime.classify", return_value=_rs("BULL")):
            ok, reason = await eng._regime_gate()
        assert ok is True and reason == ""

    async def test_bear_blocks(self, eng):
        """BEAR → (False, 含 regime 的中文 reason)。"""
        with patch("trading.compute.regime.classify",
                   return_value=_rs("BEAR", "HS300 4666≤MA200 4702")):
            ok, reason = await eng._regime_gate()
        assert ok is False and "regime" in reason and "MA200" in reason

    async def test_unknown_fail_closed(self, eng):
        """UNKNOWN → fail-closed 同 BEAR 拒（DG-G3：缺信息收紧）。"""
        with patch("trading.compute.regime.classify",
                   return_value=_rs("UNKNOWN", "指数历史不足 201 根")):
            ok, reason = await eng._regime_gate()
        assert ok is False and "regime" in reason

    async def test_classify_exception_fail_closed(self, eng):
        """classify 抛异常（import 级极端）→ 兜底拒，不炸引擎。"""
        with patch("trading.compute.regime.classify",
                   side_effect=RuntimeError("湖损坏")):
            ok, reason = await eng._regime_gate()
        assert ok is False


# ============ _pre_open_gate ④ 段 ============
class TestPreOpenGateRegime:
    async def test_gate_blocks_on_bear_when_others_green(self, eng, monkeypatch):
        """①②③ 全绿但 regime BEAR → 拒挂单（④ 段生效）。"""
        def fake_load_plan(d):   # 同步：_pre_open_gate 内 load_plan 是同步调用
            return {"confirmed": True, "entries": []}
        monkeypatch.setattr("trading.engine.load_plan", fake_load_plan)
        monkeypatch.setattr(eng, "_gw_health_gate", lambda gw: (True, ""))
        monkeypatch.setattr("trading.engine.get_ready", lambda *a, **k: True)
        with patch("trading.compute.regime.classify",
                   return_value=_rs("BEAR", "宽度 23%")):
            ok, reason = await eng._pre_open_gate("2026-08-17", object())
        assert ok is False and "regime" in reason

    async def test_gate_passes_when_all_green_including_bull(self, eng, monkeypatch):
        """①②③④ 全绿（BULL）→ 放行（既有放行语义保持）。"""
        def fake_load_plan(d):
            return {"confirmed": True, "entries": []}
        monkeypatch.setattr("trading.engine.load_plan", fake_load_plan)
        monkeypatch.setattr(eng, "_gw_health_gate", lambda gw: (True, ""))
        monkeypatch.setattr("trading.engine.get_ready", lambda *a, **k: True)
        with patch("trading.compute.regime.classify", return_value=_rs("BULL")):
            ok, reason = await eng._pre_open_gate("2026-08-17", object())
        assert ok is True and reason == ""
