# -*- coding: utf-8 -*-
"""_eod 注入 resolve_active+scan_live 单测（Task 7b · 二期 gap② 策略数据源）。

测试边界（控制器 scope #5 · 不真读 parquet / 不真起 schtasks）：
- monkeypatch ``engine._load_universe`` / ``engine._load_df_upto`` 适配 lake 参数签名
  （Task 7b fix 后两者均接收 ``lake`` DataFrame 由 _eod 入口一次性注入，本层 mock 成
  返固定 symbol 列表 / mock df，不再 mock read_parquet——保持单测在「不触盘」边界）；
- monkeypatch ``experiment.resolver.resolve_active`` 返受控实验集；
- monkeypatch ``strategies.registry.build_strategy`` 返 mock strategy（受控 scan_live 输出）；
- monkeypatch ``engine.calendar.is_trading_day`` 恒真（避开节假日判定）；
- monkeypatch ``trading_plan.push_plan_to_dingtalk`` 拦截网络副作用；
- 捕获 ``engine.eod_plan`` 入参（signals / atr_map）做断言。

What+Why：本层只验证「resolve_active → build_strategy → scan_live → 信号注入归因字段
→ 透传给 eod_plan」的胶水链路；scan_live 的识别正确性由 strategies/ 自身的测试负责，
不在本文件重复覆盖（避免耦合两层）。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

import pandas as pd
import pytest

from trading import engine, trading_plan


# ----------------------------------------------------------------------------
# 公共 fixture：每个 case 独立 TRADE_PLAN_DIR + 影子模式默认（与 test_engine 同口径）。
# ----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_plan_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")  # 影子模式默认，防测试真下单
    # 恒为交易日（节假日判定由 _eod 内 calendar.is_trading_day 负责，本文件聚焦注入链路）
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    # 拦截真发钉钉（网络副作用隔离）
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o: True)
    # 拦截 data_lake 真 read_parquet（Task 7b fix 后 _eod 入口仍会读一次 lake 作为
    # universe / df_upto 的共享源；本测试聚焦注入链路而非真盘数据，故 monkeypatch
    # pandas.read_parquet 返一个空 placeholder DataFrame，避免 455MB disk read）。
    # _load_universe / _load_df_upto 已被各 case 单独 monkeypatch，不消费此 placeholder。
    import pandas as _pd
    monkeypatch.setattr(
        _pd, "read_parquet",
        lambda *a, **kw: _pd.DataFrame(),
    )


# ============================================================================
# 1. 主链路：resolve_active 返 1 实验 → scan_live 返 1 signal → 信号携带归因字段
# ============================================================================
def test_eod_resolves_experiments_and_tags_signals(monkeypatch):
    """_eod 把 experiment_id / experiment_weight 注入每条 signal，并透传 eod_plan。"""
    from experiment.models import ActiveExperiment

    # ① 受控实验集：1 个在线颈线法实验
    fake_exp = ActiveExperiment(
        experiment_id="exp-001",
        strategy_name="neckline",
        params={"window": 20},
        weight=0.3,
    )
    monkeypatch.setattr(
        "experiment.resolver.resolve_active", lambda db_path=None: [fake_exp]
    )

    # ② mock strategy：scan_live 返 1 条信号（字段契约对齐 strategies/neckline_method.scan_live）
    class _MockStrategy:
        def __init__(self, *a, **kw):
            pass

        def scan_live(self, symbol, df_upto, date):
            # 只对 300001.SZ 返信号（验证归因字段注入）
            if symbol != "300001.SZ":
                return []
            return [{
                "symbol": symbol,
                "signal_type": "neckline",
                "formed_at": date,
                "breakout_date": date,
                "neckline": 10.5,
                "bottom": 9.5,
                "entry_price": 10.0,
                "atr": 0.25,
            }]

    monkeypatch.setattr(
        "strategies.registry.build_strategy",
        lambda name, cfg_override=None, **kw: _MockStrategy(),
    )

    # ③ 受控 universe（不走真读 parquet）——Task 7b fix 后签名加 lake 参数
    monkeypatch.setattr(engine, "_load_universe", lambda lake: ["300001.SZ", "688001.SH"])

    # ④ 受控 df_upto（≥60 行的空 OHLCV 骨架，scan_live 已被 mock 不真用字段）
    # Task 7b fix 后签名变为 (lake, symbol, date)，lake 由 _eod 入口注入（此处忽略）
    def _fake_load_df_upto(lake, symbol, date):
        idx = pd.date_range("2026-01-01", periods=80, freq="D")
        return pd.DataFrame(
            {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000},
            index=idx,
        )

    monkeypatch.setattr(engine, "_load_df_upto", _fake_load_df_upto)

    # ⑤ 捕获 eod_plan 入参（signals / atr_map）—— 直接替换 engine.eod_plan
    captured = {}

    async def _fake_eod_plan(date, signals, atr_map, capital):
        captured["date"] = date
        captured["signals"] = signals
        captured["atr_map"] = atr_map
        captured["capital"] = capital
        return {"date": date, "n_orders": len(signals), "mode": "dry_run"}

    monkeypatch.setattr(engine, "eod_plan", _fake_eod_plan)

    # 执行
    asyncio.run(engine.TradingEngine()._eod())

    # 断言：信号被注入归因字段并透传
    assert captured.get("date") == datetime.now().strftime("%Y-%m-%d")
    signals = captured.get("signals", [])
    assert len(signals) == 1
    s = signals[0]
    assert s["symbol"] == "300001.SZ"
    assert s["experiment_id"] == "exp-001"      # 归因字段注入
    assert s["experiment_weight"] == 0.3        # 权重字段注入
    # atr_map 同步建立（key=symbol, value=信号 atr）
    assert captured["atr_map"].get("300001.SZ") == 0.25


# ============================================================================
# 2. fail-fast：无在线实验 → 不调 eod_plan
# ============================================================================
def test_eod_failfast_when_no_active(monkeypatch):
    """resolve_active 返 [] → _eod 不应触达 eod_plan（fail-fast 红线）。"""
    monkeypatch.setattr("experiment.resolver.resolve_active", lambda db_path=None: [])

    # eod_plan 若被调即抛（验证未触达）
    async def _should_not_be_called(*a, **kw):
        raise AssertionError("无在线实验时 _eod 必须 fail-fast，不应调 eod_plan")

    monkeypatch.setattr(engine, "eod_plan", _should_not_be_called)

    # 仍执行（只验证不抛、不触达 eod_plan）
    asyncio.run(engine.TradingEngine()._eod())


# ============================================================================
# 3. 历史不足跳过：_load_df_upto 返 <60 行 → 该 symbol 不进 scan_live
# ============================================================================
def test_eod_skips_short_history(monkeypatch):
    """df_upto 不足 60 行的 symbol 直接跳过，scan_live 不被调（防 ATR 窗口不足）。"""
    from experiment.models import ActiveExperiment

    fake_exp = ActiveExperiment(
        experiment_id="exp-002",
        strategy_name="neckline",
        params={},
        weight=1.0,
    )
    monkeypatch.setattr(
        "experiment.resolver.resolve_active", lambda db_path=None: [fake_exp]
    )

    # scan_live 若被调即抛（验证短历史 symbol 不应触达）
    class _ShouldNotScan:
        def __init__(self, *a, **kw):
            pass

        def scan_live(self, symbol, df_upto, date):
            raise AssertionError(
                f"短历史 symbol({symbol}) 不应触达 scan_live"
            )

    monkeypatch.setattr(
        "strategies.registry.build_strategy",
        lambda name, cfg_override=None, **kw: _ShouldNotScan(),
    )

    monkeypatch.setattr(engine, "_load_universe", lambda lake: ["300001.SZ"])

    # 返 <60 行（断言 < 60 即跳过）——Task 7b fix 后签名 (lake, symbol, date)
    def _short_df(lake, symbol, date):
        idx = pd.date_range("2026-01-01", periods=30, freq="D")
        return pd.DataFrame(
            {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000},
            index=idx,
        )

    monkeypatch.setattr(engine, "_load_df_upto", _short_df)

    # eod_plan 应被调但 signals=[] （短历史 symbol 全被跳过）
    captured = {"called": False}

    async def _fake_eod_plan(date, signals, atr_map, capital):
        captured["called"] = True
        captured["signals"] = signals
        return {"date": date, "n_orders": 0, "mode": "dry_run"}

    monkeypatch.setattr(engine, "eod_plan", _fake_eod_plan)

    asyncio.run(engine.TradingEngine()._eod())

    assert captured["called"] is True
    assert captured["signals"] == []
