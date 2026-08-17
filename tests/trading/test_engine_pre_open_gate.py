# -*- coding: utf-8 -*-
"""pre_open 三段式 gate（Task 8 · C-2 S3）：plan-confirmed + gateway-health + data-ready。

物理意图（spec S3 · 三段式前置 gate，最便宜先做）：
    TradingEngine._pre_open_gate 在模块级 pre_open(date) 入口最先调用，全部绿才放行
    下游（撤昨日单 / 抓熔断基线 / 挂新单）。任一未绿即早返 skip payload，绝不触达网关
    写操作。顺序：① 计划确认（读本地 JSON）→ ② 网关健康（_connected + is_client_ready
    纯探测）→ ③ 数据就绪（DB data_ready 表查）。

测试边界（控制器 scope #5）：
    绝不真起 APScheduler / 真发钉钉 / 真下单：TradingEngine 仅实例化（装配 4 job 不
    start），load_plan / get_data_ready / _plan_data_keys 均 patch 拦截。gw 用
    MagicMock 模拟（_connected / is_client_ready 返指定值）。

TDD 约定（与 Task 7/8/9/10 一致 · 见 test_engine_order_update_handler.py:18-20 备注）：
    本仓库未配 pytest-asyncio 的 asyncio_mode，历史 engine 测试一律 ``asyncio.run(...)``
    同步驱动 async。本测试沿袭该范式，避免引入 @pytest.mark.asyncio 造成风格分叉。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from trading.engine import TradingEngine


@pytest.fixture
def eng():
    """构造 TradingEngine（仅实例化装配 4 job，不 start）。"""
    return TradingEngine()


# ============================================================================
# ① 计划确认段
# ============================================================================
def test_no_plan_blocks(eng):
    """无计划（load_plan 返 None）→ gate False，reason 含「无计划」。"""
    with patch("trading.engine.load_plan", return_value=None):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", None))
    assert ok is False
    assert "无计划" in reason


def test_unconfirmed_blocks(eng):
    """计划 confirmed=False → gate False，reason 含「未确认」。"""
    with patch("trading.engine.load_plan",
               return_value={"confirmed": False, "orders": []}):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", None))
    assert ok is False
    assert "未确认" in reason


# ============================================================================
# ② 网关健康段
# ============================================================================
def test_gateway_none_blocks(eng):
    """gw=None → gate False，reason 含「网关」。"""
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", None))
    assert ok is False
    assert "网关" in reason


def test_gateway_not_connected_blocks(eng):
    """gw._connected=False → gate False，reason 含「网关」。"""
    gw = MagicMock()
    gw._connected = False
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", gw))
    assert ok is False
    assert "网关" in reason


def test_gateway_client_not_ready_blocks(eng):
    """gw._connected=True 但 is_client_ready()=False → gate False，reason 含「客户端」。"""
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = False
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", gw))
    assert ok is False
    assert "客户端" in reason


# ============================================================================
# ③ 数据就绪段（W5 单口判定 · spec #13 T10）
# ============================================================================
# W5 改造后 gate ③ 段改调 state_store.get_ready（合成 data_ready① + job_ledger②），
# 不再分散遍历 get_data_ready。测试随之升级：mock trading.engine.get_ready（单口判定）
# 模拟三源组合结果，让 gate 测试聚焦「单口结果 → gate 决策」而非内部三源细节
# （三源组合的覆盖在 tests/trading/test_data_ready.py）。
def test_data_not_ready_blocks(eng):
    """③ get_ready=False（data_ready 未采集或 job_ledger 未 done）→ gate False，reason 含「数据」。"""
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}), \
         patch("trading.engine.TradingEngine._plan_data_keys",
               return_value={"daily"}), \
         patch("trading.engine.get_ready", return_value=False):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", gw))
    assert ok is False
    assert "数据" in reason


def test_data_ok_zero_blocks(eng):
    """③ get_ready=False（内容校验未绿 / pipeline 台账未 done）→ gate False，reason 含「数据」。

    W5：原「ok=0 + 行数不足」断言改为单口 False（具体哪源漂移由 get_ready 内部
    logger.warning 暴露，gate reason 只给汇总中文）。物理意图：gate 不再关心三源
    分散细节，只消费单口结果——守 W5「单口判定」精神（spec #13）。
    """
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}), \
         patch("trading.engine.TradingEngine._plan_data_keys",
               return_value={"daily"}), \
         patch("trading.engine.get_ready", return_value=False):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", gw))
    assert ok is False
    assert "数据" in reason


# ============================================================================
# 全绿放行
# ============================================================================
def test_all_green_passes(eng):
    """①②③ 全绿（plan confirmed + gw ready + get_ready）→ gate (True, "")。"""
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}), \
         patch("trading.engine.TradingEngine._plan_data_keys",
               return_value={"daily"}), \
         patch("trading.engine.get_ready", return_value=True):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", gw))
    assert ok is True
    assert reason == ""


def test_all_green_multi_dataset_passes(eng):
    """_plan_data_keys 返多数据集，get_ready=True（全绿）→ gate (True, "")。

    W5：多数据集的组合判定下沉到 get_ready 内部（遍历 datasets 查 data_ready），
    gate 测试只验证「keys 传入 + get_ready=True → 放行」。
    """
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}), \
         patch("trading.engine.TradingEngine._plan_data_keys",
               return_value={"daily", "moneyflow"}), \
         patch("trading.engine.get_ready", return_value=True):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", gw))
    assert ok is True
    assert reason == ""


def test_partial_dataset_blocks(eng):
    """_plan_data_keys 返多数据集，get_ready=False（其中之一未就绪）→ gate False。

    W5：原「reason 含 moneyflow」断言改为单口 False 汇总文案。具体哪源未绿由
    get_ready 内部 logger.warning 暴露（测试在 test_data_ready.py 覆盖）。
    """
    gw = MagicMock()
    gw._connected = True
    gw.is_client_ready.return_value = True
    with patch("trading.engine.load_plan",
               return_value={"confirmed": True, "orders": []}), \
         patch("trading.engine.TradingEngine._plan_data_keys",
               return_value={"daily", "moneyflow"}), \
         patch("trading.engine.get_ready", return_value=False):
        ok, reason = asyncio.run(eng._pre_open_gate("2026-07-30", gw))
    assert ok is False
    assert "数据" in reason


# ============================================================================
# _plan_data_keys：防御性默认 + resolver 反查
# ============================================================================
def test_plan_data_keys_default_when_resolve_fails(eng):
    """resolve_active 抛异常（或无实验）→ _plan_data_keys 回退默认 {"daily"}。"""
    plan = {"confirmed": True, "orders": []}
    with patch("experiment.resolver.resolve_active",
               side_effect=RuntimeError("db locked")):
        keys = eng._plan_data_keys(plan)
    assert keys == {"daily"}


def test_plan_data_keys_default_when_empty_orders(eng):
    """plan orders 为空 → 无实验可反查 → 回退默认 {"daily"}。"""
    plan = {"confirmed": True, "orders": []}
    keys = eng._plan_data_keys(plan)
    assert keys == {"daily"}


# ============================================================================
# 模块级 pre_open 集成：gate 失败早返 skip payload
# ============================================================================
def test_pre_open_skip_on_gate_failure(monkeypatch, tmp_path):
    """模块级 pre_open：gate 未通过（无计划）→ 早返 skip payload，不触达网关写。

    断言：
      1) 返回 dict 含 skipped 字段（值=gate reason）；
      2) 不调 _submit（无下单副作用）；
      3) 不调 _cancel_all_open_orders（无撤昨日单副作用）。

    注：``get_gateway`` 是惰性 singleton getter（只读，无写副作用），gate 入口允许
    调它取 gw 引用传给 ② 段判 ``_connected``；本测试只 patch 它返 None（让 ② 段也
    会 fail），不把它列为「不应触达」守卫——因为它不是网关写操作。
    """
    from trading import engine
    # W1-A/T2-Task11：engine.pre_open 是 ``from trading.phases.pre_open import pre_open``
    # 别名（engine.py:179），调用链全程在 phases.pre_open 本地解析。_pre_open_impl 内
    # get_gateway / _submit / _cancel_all_open_orders 经 phases.pre_open 顶部 ``from…import``
    # 本地绑定读（pre_open.py:107/95）→ monkeypatch engine.X 不命中 → 迁 _pre_open_mod.X。
    # 对比：load_plan / get_ready（C4 不迁）由 _pre_open_gate（engine.py:678 实例方法）经
    # engine 模块全局名读 → patch trading.engine.load_plan 命中 → 保 engine（见下文 line 252）。
    import trading.phases.pre_open as _pre_open_mod

    # 隔离 plan dir + state_store db（与 test_engine.py 同款隔离）
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    from trading import state_store, position_book
    _db = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", _db)
    monkeypatch.setattr(state_store, "_DEFAULT_DB", _db)
    position_book.init_db()
    state_store.init_store()

    # T1：gate 经 ports.gate 调用实例方法。eng._ports 在 __init__ 构造（gate=lambda→
    # self._pre_open_gate），pre_open 显式传 ports=eng._ports 让三段闸生效。
    eng = TradingEngine()

    # get_gateway 返 None（惰性 singleton getter，只读无写副作用，允许调用）。
    # W1-A/T2-Task11：迁 phases.pre_open 本地绑定——_pre_open_impl:221 ``ports.gate(date,
    # get_gateway())`` 经 phases 模块全局名读，patch engine.get_gateway 不命中调用链。
    monkeypatch.setattr(_pre_open_mod, "get_gateway", lambda: None)

    # 守卫：_submit / _cancel_all_open_orders 绝不应被调（gate 拦截即早返）
    async def _no_submit(*a, **kw):
        raise AssertionError("gate 未通过绝不应触达 _submit")

    async def _no_cancel(gw):
        raise AssertionError("gate 未通过绝不应触达 _cancel_all_open_orders")

    # W1-A/T2-Task11：迁 phases.pre_open 本地绑定——_pre_open_impl 读 phases 模块全局名
    # （_submit pre_open.py:107 import / _cancel_all_open_orders pre_open.py:95 import as
    # 别名），patch engine._submit / engine._cancel_all_open_orders 不命中调用链
    # （engine 侧虽 re-export 同名属性，但 phases 调用链不经 engine 模块全局名解析）。
    monkeypatch.setattr(_pre_open_mod, "_submit", _no_submit)
    monkeypatch.setattr(_pre_open_mod, "_cancel_all_open_orders", _no_cancel)

    # load_plan 返 None → gate ① 段失败
    with patch("trading.engine.load_plan", return_value=None):
        result = asyncio.run(engine.pre_open("2026-07-30", ports=eng._ports))

    # 返回 shape 已规范化为与 pre_open 其它返回一致（success: {"submitted","mode"}；
    # skip: {"submitted","reason"}）。gate skip 保留 skipped（携带 gate reason）+ 补
    # submitted/mode，让任何读 result["submitted"] 的调用方不 KeyError（T8 M1 修复）。
    assert result["submitted"] == 0
    assert result["mode"] == "dry_run"
    assert result["skipped"] == "无计划"
