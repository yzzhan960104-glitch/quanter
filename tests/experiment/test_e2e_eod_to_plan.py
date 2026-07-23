# -*- coding: utf-8 -*-
"""端到端（dry_run）归因不断链测试：experiment CLI create → promote → resolver
→ TradingEngine._eod（resolve_active + scan_live + 实验归因注入）
→ signal_runner.build_orders_from_signals → PlannedOrder 携带归因字段。

物理意图（design v2 §12 验收 2 红线）：
    实验系统 Task 1-8 已全部就绪：experiment 包（配置中心）+ signal_runner（归因+权重）
    + eod_plan（透传）+ _eod（resolve_active/scan_live 注入）+ report（聚合）。本文件
    作为 Task 9 端到端集成测试，**不 mock 实验子系统本身**（真起 SQLite + CLI create/promote
    让 resolver 真读到 ACTIVE 实验），只 mock 外围副作用（data_lake parquet / 网络 / 钉钉），
    验证 ``experiment_id`` / ``experiment_weight`` 从「实验创建」一路透传到 ``PlannedOrder``，
    全程不丢字段、不串号——这是归因审计闭环（Task 8 report）能成立的物理基础。

测试边界（控制器 scope #5 · 不真读盘 / 不真发单 / 不真推钉钉）：
    - 真实验子系统：``store.init_db`` 建临时 SQLite + ``cli.main`` 真建真 promote，
      resolver 实时读 db 返 [ActiveExperiment]（这是端到端「真」链路的核心）；
    - mock data_lake：``pd.read_parquet`` 返占位空 DataFrame（_eod 入口读一次），
      ``_load_universe(lake)`` / ``_load_df_upto(lake, sym, date)`` 各自 mock 成受控返回
      （Task 7b fix 后两者签名接 ``lake`` 由 _eod 注入，此处适配真实签名）；
    - mock 策略：``build_strategy`` 返 mock strategy，``scan_live`` 返受控信号 dict（对齐
      strategies/neckline_method.scan_live 字段契约）；
    - mock 网络/落盘副作用：``trading_plan.push_plan_to_dingtalk`` / ``calendar.is_trading_day``
      均 monkeypatch，保证测试不触达真实网络/调度；
    - **不** mock ``signal_runner.build_orders_from_signals``：让它真跑（验证归因字段从
      signal → PlannedOrder 的透传链路，这是本测试的核心断言对象）。

What+Why：
    brief（task-9-brief.md）基于旧 plan v2 写的 scan_at + 无参 _load_universe，已与 Task 7b
    实际实现（scan_live + lake 注入）漂移。本文件按 Task 7b 真实签名适配（参考
    ``tests/trading/test_engine_eod_injection.py`` 的 monkeypatch 范式），不改实现迁就测试。
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from experiment import cli, resolver, store
from trading import engine, trading_plan


# ============================================================================
# Fixture：临时 SQLite + 外围副作用隔离
# ============================================================================
@pytest.fixture
def db(tmp_path, monkeypatch):
    """临时实验 db：monkeypatch experiment.store/resolver 的 _DEFAULT_DB。

    Why 真起 SQLite：端到端归因测试的核心价值就是「真 CLI create/promote → resolver
    真读到 ACTIVE 实验」，mock 掉 store 就退化成 Task 7b 单测，失去 e2e 意义。
    双 patch（store + resolver）原因见 tests/experiment/test_cli.py::db fixture 注释——
    resolver 模块自己 import 了一份 _DEFAULT_DB 引用，单 patch store 不够。
    """
    p = str(tmp_path / "e2e.db")
    store.init_db(p)
    monkeypatch.setattr(store, "_DEFAULT_DB", p)
    monkeypatch.setattr(resolver, "_DEFAULT_DB", p)
    monkeypatch.setattr(cli, "_DEFAULT_DB", p)  # CLI 入口模块也持有 _DEFAULT_DB 引用
    return p


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch, tmp_path):
    """每个 case 独立的运行时环境隔离（autouse）。

    Why autouse：本文件所有 case 共享同一套外围副作用隔离，避免每 case 重复 patch；
    且确保即便有新 case 忘记 patch 也不会触达真盘/真网。

    覆盖项（与 tests/trading/test_engine_eod_injection.py 同口径）：
    - ``TRADE_PLAN_DIR`` → tmp_path（save_plan 落盘到临时目录，不污染 logs/）；
    - ``AUTO_TRADE_MODE`` → dry_run（影子模式，pre_open 不会走 live 路径——本测试虽
      不调 pre_open，但环境干净是工程纪律）；
    - ``calendar.is_trading_day`` → 恒真（绕开节假日判定，_eod 入口的交易日闸门）；
    - ``trading_plan.push_plan_to_dingtalk`` → no-op（拦截真发钉钉网络副作用）；
    - ``pd.read_parquet`` → 返空 DataFrame（_eod 入口会读一次 data_lake，避免 455MB disk read）。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    # 恒交易日：绕开节假日闸门，聚焦归因链路验证（节假日逻辑由 test_calendar 覆盖）
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    # 拦截钉钉推送：网络副作用隔离（push_plan_to_dingtalk 调 dws send-by-bot）
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o: True)
    # 拦截 data_lake parquet 读：_eod 入口读一次，mock 成空 placeholder；
    # _load_universe / _load_df_upto 各 case 单独 mock，不消费此 placeholder。
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **kw: pd.DataFrame())


# ============================================================================
# 辅助：mock 策略（受控 scan_live 输出）
# ============================================================================
def _make_mock_strategy_factory(captured_signals=None):
    """构造 ``build_strategy`` 的 mock 替代函数。

    Args:
        captured_signals: 可选 list，strategy.scan_live 被调时会 append 进该 list,
                          供测试断言「scan_live 真被 _eod 调用且收到正确入参」。

    物理意图：模拟 strategies/neckline_method.scan_live 的字段契约（ NecklineMethodStrategy
    实例方法 ``scan_live(symbol, df_upto, date) -> list[Signal]``，Layer2 阶段1 后返
    Signal dataclass），只对 1 个标的返 1 条信号，便于精准断言归因字段注入。

    返回的 mock strategy 与真实策略的字段对齐：
        Signal(symbol, signal_type, formed_at, breakout_date, neckline, bottom, entry_price, atr)
    其中 ``atr`` 字段 _eod 会读并建 atr_map（缺 atr 不建项，signal_runner 会跳过该 symbol）。
    """
    from strategies.signal import Signal

    class _MockStrategy:
        def __init__(self, *args, **kwargs):
            pass

        def scan_live(self, symbol, df_upto, date):
            if captured_signals is not None:
                captured_signals.append({"symbol": symbol, "date": date})
            # 只对 300001.SZ 返信号（其他标的返空，验证归因只挂到真信号上）
            if symbol != "300001.SZ":
                return []
            return [Signal(
                symbol=symbol,
                signal_type="neckline",
                formed_at=date,
                breakout_date=date,
                neckline=10.5,
                bottom=9.5,
                entry_price=10.0,
                atr=0.5,  # 非 None/0/NaN，_eod 会建 atr_map[symbol]=0.5
            )]

    def _factory(name, cfg_override=None, **kwargs):
        return _MockStrategy()

    return _factory


# ============================================================================
# 端到端：experiment_id 从创建 → signal → PlannedOrder 全程携带
# ============================================================================
def test_e2e_attribution_chain(db, tmp_path, monkeypatch):
    """全链路归因不断链：experiment CLI create/promote → resolver → _eod →
    scan_live → signal_runner.build_orders_from_signals → PlannedOrder 携带
    experiment_id="e1" + experiment_weight=1.0 + qty 整百。

    验证矩阵（design v2 §12 验收 2）：
        1. CLI create → store 真写 DRAFT；
        2. CLI promote weight=1.0 → store 真迁 ACTIVE + 权重写入；
        3. resolver.resolve_active()（无参，读 _DEFAULT_DB）真读到该实验；
        4. _eod 真跑：resolve_active → scan_live → 归因字段注入 signal → eod_plan；
        5. signal[0] 带 experiment_id/experiment_weight（_eod 注入）；
        6. PlannedOrder[0] 带 experiment_id/experiment_weight（signal_runner 透传）；
        7. qty%100==0（A 股整手红线，向下取整 100）；
        8. atr_map 正确建立（key=symbol, value=signal atr）。
    """
    # ---------- ① 真实验子系统：CLI create → promote ----------
    rc = cli.main([
        "create", "--strategy", "neckline",
        "--params", '{"window": 60}',
        "--experiment-id", "e1",
        "--created-at", "2026-07-22T10:00:00",
    ])
    assert rc == 0, "CLI create 应成功"

    rc = cli.main(["promote", "e1", "--weight", "1.0"])
    assert rc == 0, "CLI promote weight=1.0 应成功"

    # resolver 实时读 db 应真读到 ACTIVE 实验（端到端「真」链路核心断言）
    actives = resolver.resolve_active()
    assert len(actives) == 1
    assert actives[0].experiment_id == "e1"
    assert actives[0].weight == 1.0

    # ---------- ② mock 策略 + 数据源外围（适配 Task 7b 真实签名） ----------
    scan_calls = []  # 捕获 scan_live 入参（symbol/date），断言 _eod 真调到
    monkeypatch.setattr(
        "strategies.registry.build_strategy",
        _make_mock_strategy_factory(captured_signals=scan_calls),
    )

    # _load_universe(lake) → Task 7b 签名带 lake 参数（_eod 注入），mock 成受控 universe
    monkeypatch.setattr(engine, "_load_universe", lambda lake: ["300001.SZ", "688001.SH"])

    # _load_df_upto(lake, symbol, date) → 返 ≥60 行 OHLCV 骨架（scan_live 已 mock 不真用字段，
    # 但 _eod 内有 len(df_upto) < 60 跳过判断，故必须 ≥60 行才能进 scan_live）
    def _fake_load_df_upto(lake, symbol, date):
        idx = pd.date_range("2026-01-01", periods=80, freq="D")
        return pd.DataFrame(
            {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000},
            index=idx,
        )

    monkeypatch.setattr(engine, "_load_df_upto", _fake_load_df_upto)

    # ---------- ③ 捕获 eod_plan 入参（signals/atr_map/capital），替换 engine.eod_plan ----------
    captured = {}

    async def _fake_eod_plan(date, signals, atr_map, capital):
        """直接替换 engine.eod_plan，不走真 save_plan/push，改跑 build_orders_from_signals
        验证归因透传（signal → PlannedOrder 的关键跃迁点）。

        Why 真 build：本测试的核心断言对象是 PlannedOrder 上的 experiment_id/weight，
        必须**真跑** signal_runner.build_orders_from_signals，不能 mock——mock 掉就退化
        成「只验证 _eod 注入」，无法证明归因能落到下游订单。build 签名对齐 Task 5：
        build_orders_from_signals(signals, *, capital, pos_cap, atr_map, stop_cfg)。
        """
        captured["date"] = date
        captured["signals"] = signals
        captured["atr_map"] = atr_map
        captured["capital"] = capital
        # 真跑 compute.plan 真身（Task 5 归因透传链路），用 _trade_cfg 缺省口径
        # Layer2 阶段6 follow-up #4a：signal_runner 垫片已删，直指真身 trading.compute.plan
        from trading.compute.plan import build_orders_from_signals
        captured["orders"] = build_orders_from_signals(
            signals,
            capital=capital,
            pos_cap=0.05,
            atr_map=atr_map,
            stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0},
        )
        return {"date": date, "n_orders": len(captured["orders"]), "mode": "dry_run"}

    monkeypatch.setattr(engine, "eod_plan", _fake_eod_plan)

    # ---------- ④ 执行 _eod（绕开 __init__ 的 APScheduler 装配，直接调 async 方法） ----------
    # 用 __new__ 跳过 __init__（构造会起 AsyncIOScheduler，e2e 测试不需要 cron 装配）。
    # 这与 brief/Task 7b 单测同范式，避免 import apscheduler 副作用。
    eng = engine.TradingEngine.__new__(engine.TradingEngine)
    asyncio.run(eng._eod())

    # ---------- ⑤ 断言：归因从「实验创建」到「PlannedOrder」全程不断链 ----------
    # 5.1 _eod 真调到了 scan_live（验证 universe 内目标标的触达扫描）
    assert scan_calls, "_eod 应调用 scan_live 至少一次"
    scanned_syms = {c["symbol"] for c in scan_calls}
    assert "300001.SZ" in scanned_syms, "300001.SZ 应被 scan_live 扫到"

    # 5.2 signal 层：归因字段被 _eod 注入
    signals = captured.get("signals", [])
    assert len(signals) == 1, "300001.SZ 应产 1 条信号（688001.SH 无信号）"
    s = signals[0]
    # Layer2 阶段1：signals 现为 list[Signal]（frozen dataclass），读属性验证归因注入
    assert s.symbol == "300001.SZ"
    assert s.experiment_id == "e1",         "归因字段 experiment_id 未注入 signal"
    assert s.experiment_weight == 1.0,      "归因字段 experiment_weight 未注入 signal"

    # 5.3 atr_map 建立（key=symbol, value=signal atr，build_orders 据此算 stop_price）
    assert captured["atr_map"].get("300001.SZ") == 0.5

    # 5.4 PlannedOrder 层：归因透传（signal → PlannedOrder 的关键跃迁点）
    orders = captured.get("orders", [])
    assert len(orders) == 1, "build_orders_from_signals 应产 1 笔 PlannedOrder"
    o = orders[0]
    assert o.experiment_id == "e1",            "PlannedOrder 未携带 experiment_id（归因断链）"
    assert o.experiment_weight == 1.0,         "PlannedOrder 未携带 experiment_weight（归因断链）"

    # 5.5 A 股整手红线：qty 必须是 100 的整数倍（向下取整 100 整手）
    # budget = 1_000_000 × 0.05 × 1.0 = 50000；qty = floor(50000 / 10.0 / 100) × 100 = 5000
    assert o.order.qty % 100 == 0, f"qty={o.order.qty} 非整百（A 股整手红线违反）"
    assert o.order.qty > 0, "qty 必须为正（资金足以挂 ≥1 手）"

    # 5.6 止损价口径：neckline − stop_mult × atr = 10.5 − 2.0 × 0.5 = 9.5
    assert o.stop_price == pytest.approx(9.5)
    # 止盈价口径：neckline + tp_mult × H；H = neckline − bottom = 1.0；tp = 10.5 + 2.0 × 1.0 = 12.5
    assert o.take_profit == pytest.approx(12.5)


# ============================================================================
# 端到端负向：多实验权重分流（验证灰度场景下每个 PlannedOrder 各自带正确归因）
# ============================================================================
def test_e2e_multi_experiment_attribution_split(db, monkeypatch):
    """多实验同时 ACTIVE：每实验独立归因，weight 在 PlannedOrder 上各自冻结正确。

    Why 这个 case：Task 5 灰度分流的物理语义是「多实验同跑，各自按 weight 分资金」，
    必须验证不同 experiment_id 的 signal 落到不同 PlannedOrder 时，归因字段不会串号
    （不会把 e1 的归因误挂到 e2 的单子上）。这是 Task 8 report 能正确按实验聚合的前提。

    场景：e1 weight=0.5（颈线法，params={"window":60}）+ e2 weight=0.5（颈线法，params={"window":90}）。
    两个实验都打到同一标的 300001.SZ（scan_live 同一 mock 对两实验都返信号），验证
    两条 PlannedOrder 各自带自己实验的 experiment_id/weight。
    """
    # ① 建两实验（不同 version 同 strategy_name，schema UNIQUE(strategy_name, version) 不冲突）
    cli.main(["create", "--strategy", "neckline", "--params", '{"window": 60}',
              "--experiment-id", "e1", "--version", "1", "--created-at", "t"])
    cli.main(["create", "--strategy", "neckline", "--params", '{"window": 90}',
              "--experiment-id", "e2", "--version", "2", "--created-at", "t"])
    cli.main(["promote", "e1", "--weight", "0.5"])
    cli.main(["promote", "e2", "--weight", "0.5"])  # 0.5 + 0.5 = 1.0 ≤ 1.0 资金守恒红线

    actives = resolver.resolve_active()
    assert {a.experiment_id for a in actives} == {"e1", "e2"}

    # ② mock 策略：对每实验的 300001.SZ 都返信号（atr=0.5）
    # 把每条 signal 打上「来自哪个实验」的内嵌标记（_eod 会随后覆盖 experiment_id）
    from strategies.signal import Signal

    class _SplitStrategy:
        def __init__(self, *args, **kwargs):
            pass

        def scan_live(self, symbol, df_upto, date):
            if symbol != "300001.SZ":
                return []
            return [Signal(
                symbol=symbol, signal_type="neckline",
                formed_at=date, breakout_date=date,
                neckline=10.5, bottom=9.5, entry_price=10.0, atr=0.5,
            )]

    monkeypatch.setattr(
        "strategies.registry.build_strategy",
        lambda name, cfg_override=None, **kw: _SplitStrategy(),
    )
    monkeypatch.setattr(engine, "_load_universe", lambda lake: ["300001.SZ"])
    monkeypatch.setattr(
        engine, "_load_df_upto",
        lambda lake, symbol, date: pd.DataFrame(
            {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000},
            index=pd.date_range("2026-01-01", periods=80, freq="D"),
        ),
    )

    captured = {}

    async def _fake_eod_plan(date, signals, atr_map, capital):
        # Layer2 阶段6 follow-up #4a：signal_runner 垫片已删，直指真身 trading.compute.plan
        from trading.compute.plan import build_orders_from_signals
        captured["signals"] = signals
        captured["orders"] = build_orders_from_signals(
            signals, capital=capital, pos_cap=0.05, atr_map=atr_map,
            stop_cfg={"stop_atr_mult": 2.0, "tp_h_mult": 2.0},
        )
        return {"n_orders": len(captured["orders"])}

    monkeypatch.setattr(engine, "eod_plan", _fake_eod_plan)

    eng = engine.TradingEngine.__new__(engine.TradingEngine)
    asyncio.run(eng._eod())

    # 断言：2 条 signal、2 笔 PlannedOrder，归因两两对应不串号
    signals = captured["signals"]
    orders = captured["orders"]
    assert len(signals) == 2 and len(orders) == 2

    sig_eids = {s.experiment_id for s in signals}
    ord_eids = {o.experiment_id for o in orders}
    assert sig_eids == {"e1", "e2"}, "signal 归因应覆盖两实验"
    assert ord_eids == {"e1", "e2"}, "PlannedOrder 归因应覆盖两实验（不串号）"

    # 每笔 PlannedOrder 的 weight 必须是 0.5（与各自 experiment.weight 冻结一致）
    for o in orders:
        assert o.experiment_weight == 0.5
        assert o.order.qty % 100 == 0  # 灰度小权重仍走整手规则
