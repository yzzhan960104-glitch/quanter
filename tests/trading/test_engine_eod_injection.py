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
import sqlite3
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
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
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

    # ② mock strategy：scan_live 返 1 条 Signal（字段契约对齐 strategies/neckline/strategy.scan_live）
    from strategies.neckline.signal import Signal

    class _MockStrategy:
        def __init__(self, *a, **kw):
            pass

        def scan_live(self, symbol, df_upto, date):
            # 只对 300001.SZ 返信号（验证归因字段注入）
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
                atr=0.25,
            )]

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

    # 断言：落盘 date = next_trading_day(today)，修 date 错位 bug（2026-07-28）。
    # 原 assert == today 固化了「_eod 用当天落盘」的 bug——pre_open 次日读 plan_次日
    # 读不到。fixture 已 mock is_trading_day 恒真，故 next_trading_day(today) = today+1。
    today = datetime.now().strftime("%Y-%m-%d")
    assert captured.get("date") == engine.calendar.next_trading_day(today)
    assert captured.get("date") != today  # 铁证：生效日必须晚于盘后落盘日
    signals = captured.get("signals", [])
    assert len(signals) == 1
    s = signals[0]
    # Layer2 阶段1：signals 现为 list[Signal]（frozen dataclass），读属性验证归因注入
    assert s.symbol == "300001.SZ"
    assert s.experiment_id == "exp-001"      # 归因字段注入（_eod 用 dataclasses.replace）
    assert s.experiment_weight == 0.3        # 权重字段注入
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


# ============================================================================
# 4. I1 · final-fix：_load_df_upto 真实 .loc[:date] 切片无前视直测（不 mock）
# ============================================================================
def test_load_df_upto_no_lookahead():
    """直接对真实 `_load_df_upto` 测 ``.loc[:date]`` 闭区间切片（date 是 str）。

    物理意图（I1 红线）：本文件其它 case 全部 mock ``_load_df_upto``，故
    ``lake.xs(symbol).sort_index().loc[:date]``（date 是 str）从未对真实
    ``datetime64[ns]`` MultiIndex 测过。无前视是回测/实盘的致命红线（CLAUDE.md），
    若 pandas ``.loc[:str_date]`` 行为变化（如改为开区间、或字符串解析失败），
    所有依赖它的策略信号都会被静默污染——必须直接测试。

    断言三连：
        ① 含 date 当日（闭区间）；
        ② 排除 date+1（防未来 K 线泄漏）；
        ③ 返回 df 的 index.max() <= Timestamp(date)（无前视铁证）。
    """
    # 构造合成 datetime64[ns] MultiIndex(date,symbol) lake（不 mock）
    dates = pd.date_range("2026-07-18", periods=5, freq="D")  # 18~22 共 5 个交易日
    rows = []
    for d in dates:
        # 同日塞两个 symbol，验证 xs(level="symbol") 正确取片
        for sym in ("300001.SZ", "688001.SH"):
            rows.append((d, sym, 10.0, 10.5, 9.5, 10.2, 1000))
    lake = pd.DataFrame(
        rows, columns=["date", "symbol", "open", "high", "low", "close", "volume"]
    ).set_index(["date", "symbol"])

    # 直接调真实 _load_df_upto（date 是 str，模拟 _eod 真实调用约定）
    df_upto = engine._load_df_upto(lake, "300001.SZ", "2026-07-21")

    # ① 必须返回 DataFrame（非 None）
    assert df_upto is not None, "300001.SZ 在 lake 中应能 xs 取片成功"
    # ② 含 date 当日 K 线（.loc[:date] 是闭区间）
    assert pd.Timestamp("2026-07-21") in df_upto.index, "loc[:date] 应含 date 当日（闭区间）"
    # ③ 排除 date+1（无前视红线 · 核心断言）
    assert pd.Timestamp("2026-07-22") not in df_upto.index, "loc[:date] 不应含 date 之后（无前视）"
    # ④ index.max() <= Timestamp(date)（无前视铁证 · pandas 行为兜底）
    assert df_upto.index.max() <= pd.Timestamp("2026-07-21"), \
        "df_upto.index.max() 必须 <= date（无前视红线）"
    # ⑤ 只取到指定 symbol（xs level 切片正确，不串号）
    #    DatetimeIndex 单 level 后 .index 不再有 symbol level，靠行数核验
    #    （18~21 共 4 个交易日，每日该 symbol 1 行 → 4 行）
    assert len(df_upto) == 4, f"截至 2026-07-21（含）应有 4 根 K 线，实际 {len(df_upto)}"


# ============================================================================
# 5. Task 5（P0-5 cooldown · SSoT C2b）：_eod scan 后按 trade_event SIGNAL.formed_at
#    查最近 cooldown 自然日已发信号标的集，同标的丢弃（防连续日超额成交）。
#    切换：原扫 plan_*.json formed_at（C2b 前）→ 现 substr(json_extract(meta,'$.formed_at'),1,10)
#    IN (最近 N 自然日) 查 DB（C2b）。致命日期轴：meta.formed_at = str(pd.Timestamp) =
#    "2026-08-03 00:00:00"（带时间戳，method_v0.py:268 W.index[-1] → plan.py:158 str(s.formed_at)），
#    必须 substr(1,10) 取前 10 字符（YYYY-MM-DD）匹配纯日期 IN 列表，否则恒空。
# ============================================================================

def test_load_recent_plan_symbols_by_formed_at(tmp_db):
    """_load_recent_plan_symbols 按 meta.formed_at 查 DB（C2b 单测，验证 substr(1,10) 红线）。

    红线验证：formed_at 用**生产格式 "2026-08-03 00:00:00"**（带时间戳，非纯日期）。
    若查询漏 substr(1,10) 直接 json_extract IN ('2026-08-03') 恒空 → 测试红；
    substr(1,10)="2026-08-03" IN ('2026-08-03','2026-08-04','2026-08-05') 命中 → 绿。
    纯日期测试无法暴露此坑（恰是 brief 反复强调的红线）。
    """
    import json
    from trading import state_store, engine
    # 插一行 SIGNAL，meta.formed_at 用生产格式（带时间戳），plan_date=T+1
    state_store.insert_trade_event(
        "ACC_TEST",
        state_store.build_trade_id("ACC_TEST", "A.SH", "2026-08-05"),
        "A.SH", "SIGNAL",
        meta=json.dumps({"formed_at": "2026-08-03 00:00:00", "plan_date": "2026-08-05"}),
    )
    # days_back=3, today=2026-08-05 → 窗口 [08-03, 08-04, 08-05]（含 08-03 formed_at）
    syms = engine._load_recent_plan_symbols(days_back=3, today="2026-08-05")
    assert "A.SH" in syms, f"formed_at=2026-08-03 00:00:00 应被 substr(1,10) 命中，实际 {syms}"

    # 反向验证：days_back=2（窗口 08-04~08-05，不含 08-03）→ 不命中
    syms_out = engine._load_recent_plan_symbols(days_back=2, today="2026-08-05")
    assert "A.SH" not in syms_out, f"窗口外应漏，实际 {syms_out}"


def test_load_recent_plan_symbols_db_error_degrades_empty(tmp_db, monkeypatch):
    """DB 异常 → 返空集（保守 cooldown 不去重，比崩好）。"""
    from trading import engine, state_store

    def _boom(dates, *, db_path=None):
        raise sqlite3.OperationalError("模拟 DB 损坏")

    monkeypatch.setattr(state_store, "list_signal_symbols_by_formed_at", _boom)
    # 不抛即降级（logger.exception 记录但返空集，让所有新信号通过）
    syms = engine._load_recent_plan_symbols(days_back=3, today="2026-08-05")
    assert syms == set()


def test_eod_cooldown_dedup_recent_signal_dropped(monkeypatch, tmp_db):
    """同标的最近 cooldown 日已发 SIGNAL（meta.formed_at 在窗口内）→ 新信号丢弃。

    C2b 迁移：原构造 plan_*.json formed_at → 现插 trade_event SIGNAL 行（SSoT）。
    物理意图不变：scan_live 无跨日去重，同形态在多日窗口内连续触发（颈线被持续突破），
    实盘会连续挂单超额成交。spec §4.5：_eod scan 后查最近 cooldown 日 SIGNAL.formed_at，
    同标的丢弃。

    场景：2 日前插一行 300001.SZ SIGNAL（formed_at 在 cooldown=5+2=7 自然日窗口内）→
          新信号 300001.SZ 丢弃，688001.SH 保留。
    """
    import json
    import sqlite3  # noqa: F401  （test_load_recent_plan_symbols_db_error_degrades 用）
    from datetime import datetime, timedelta
    from experiment.models import ActiveExperiment
    from strategies.neckline.signal import Signal
    from trading import state_store

    fake_exp = ActiveExperiment(
        experiment_id="exp-cooldown", strategy_name="neckline",
        params={"cooldown": 5}, weight=1.0,
    )
    monkeypatch.setattr(
        "experiment.resolver.resolve_active", lambda db_path=None: [fake_exp]
    )

    # Mock strategy：返两条信号，cooldown 测试在 _eod 层做（不在 strategy 内）
    class _MockStrategy:
        def __init__(self, *a, **kw):
            pass

        def scan_live(self, symbol, df_upto, date):
            # 两条信号：A 在 cooldown 内（应丢），B 超 cooldown（应留）
            today_ts = pd.Timestamp(date)
            return [
                Signal(symbol="300001.SZ", formed_at=today_ts, neckline=10.0,
                       bottom=9.0, entry_price=10.0, atr=0.2),
                Signal(symbol="688001.SH", formed_at=today_ts, neckline=20.0,
                       bottom=18.0, entry_price=20.0, atr=0.4),
            ]

    monkeypatch.setattr(
        "strategies.registry.build_strategy",
        lambda name, cfg_override=None, **kw: _MockStrategy(),
    )
    monkeypatch.setattr(engine, "_load_universe", lambda lake: ["300001.SZ"])
    monkeypatch.setattr(engine, "_load_df_upto", lambda lake, s, d: pd.DataFrame(
        {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000},
        index=pd.date_range("2026-01-01", periods=80, freq="D"),
    ))

    # C2b：构造历史 SIGNAL（替代原 plan_*.json）。2 日前插 300001.SZ（cooldown=5+2 自然日窗口内）
    # 固定 today 便于窗口断言（避免跨日漂移）；_eod 内部用 clock.today() 取今天。
    today = datetime.now().strftime("%Y-%m-%d")
    today_ts = datetime.strptime(today, "%Y-%m-%d")
    recent_date = (today_ts - timedelta(days=2)).strftime("%Y-%m-%d")  # 2 日前（cooldown=5 内）
    old_date = (today_ts - timedelta(days=10)).strftime("%Y-%m-%d")   # 10 日前（cooldown 外）

    # 关键：formed_at 用生产格式（带时间戳），plan_date=T+1（build_trade_id 单点口径）
    state_store.insert_trade_event(
        "ACC_TEST",
        state_store.build_trade_id("ACC_TEST", "300001.SZ", recent_date),
        "300001.SZ", "SIGNAL",
        meta=json.dumps({
            "formed_at": f"{recent_date} 00:00:00",  # 生产格式（pd.Timestamp str 落盘）
            "plan_date": recent_date,
        }),
    )

    captured = {}

    async def _fake_eod_plan(date, signals, atr_map, capital):
        captured["signals"] = signals
        return {"date": date, "n_orders": len(signals), "mode": "dry_run"}

    monkeypatch.setattr(engine, "eod_plan", _fake_eod_plan)

    asyncio.run(engine.TradingEngine()._eod())

    # 300001.SZ 在 cooldown 内被丢弃；688001.SH 保留
    syms = [s.symbol for s in captured["signals"]]
    assert "300001.SZ" not in syms, "同标的 cooldown 内应丢弃"
    assert "688001.SH" in syms


def test_eod_cooldown_dedup_old_signal_kept(monkeypatch, tmp_db):
    """超 cooldown 日的历史 SIGNAL 不影响新信号（超窗口查不到，标的可重出信号）。

    C2b 迁移：原 10 日前 plan_*.json → 现 10 日前 trade_event SIGNAL（窗口外查不到）。
    """
    import json
    from datetime import datetime, timedelta
    from experiment.models import ActiveExperiment
    from strategies.neckline.signal import Signal
    from trading import state_store

    fake_exp = ActiveExperiment(
        experiment_id="exp-cooldown2", strategy_name="neckline",
        params={"cooldown": 5}, weight=1.0,
    )
    monkeypatch.setattr(
        "experiment.resolver.resolve_active", lambda db_path=None: [fake_exp]
    )

    class _MockStrategy:
        def __init__(self, *a, **kw):
            pass

        def scan_live(self, symbol, df_upto, date):
            return [Signal(symbol="300001.SZ", formed_at=pd.Timestamp(date),
                           neckline=10.0, bottom=9.0, entry_price=10.0, atr=0.2)]

    monkeypatch.setattr(
        "strategies.registry.build_strategy",
        lambda name, cfg_override=None, **kw: _MockStrategy(),
    )
    monkeypatch.setattr(engine, "_load_universe", lambda lake: ["300001.SZ"])
    monkeypatch.setattr(engine, "_load_df_upto", lambda lake, s, d: pd.DataFrame(
        {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000},
        index=pd.date_range("2026-01-01", periods=80, freq="D"),
    ))

    # 10 日前插 SIGNAL（cooldown=5+2=7 自然日窗口外 → 查不到 → 新信号保留）
    today = datetime.now().strftime("%Y-%m-%d")
    old_date = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    state_store.insert_trade_event(
        "ACC_TEST",
        state_store.build_trade_id("ACC_TEST", "300001.SZ", old_date),
        "300001.SZ", "SIGNAL",
        meta=json.dumps({"formed_at": f"{old_date} 00:00:00", "plan_date": old_date}),
    )

    captured = {}

    async def _fake_eod_plan(date, signals, atr_map, capital):
        captured["signals"] = signals
        return {"date": date, "n_orders": len(signals), "mode": "dry_run"}

    monkeypatch.setattr(engine, "eod_plan", _fake_eod_plan)

    asyncio.run(engine.TradingEngine()._eod())

    # 10 日前 SIGNAL 超 cooldown=5 → 300001.SZ 保留
    syms = [s.symbol for s in captured["signals"]]
    assert "300001.SZ" in syms


# ============================================================================
# 6. Task 7（W0/D1 ② 告警通道）：filter_universe_by_continuity 过滤标的数
#    ≥ _CONTINUITY_FILTER_ALERT_FLOOR → _alert_critical 推钉钉（残数据有声）。
#    物理意图（spec §3.3.5 ②）：原 data/integrity.py:413 filter 仅 _log.warning，
#    残数据标的被过滤无声无息——若 lake 大面积漏采（如停牌复牌段批量缺失），
#    universe 静默收窄、信号锐减、研究员查盘后才察觉。本通道在 engine 调用方接
#    _alert_critical（CRITICAL 钉钉），data 层零侵入（不 import infra.notifier）。
#    节流：filter 在 exp 循环内，用 _continuity_alerted_this_eod 局部变量保每次 _eod
#    至多告警一次（N 个 exp 都达阈值也不风暴）。
# ============================================================================

@pytest.mark.parametrize(
    "n_exps, keep, expected_alerts, drop_in_msg",
    [
        # 过滤 7 ≥ 阈值 5 → 告警一次，msg 含过滤数
        (1, 3, 1, "7"),
        # 3 个 exp 都达阈值 → 局部节流只告警一次（防 N exp 钉钉风暴）
        (3, 3, 1, None),
        # 过滤 2 < 阈值 5 → 不告警（单标的偶发漏采只 warning，不刷屏）
        (1, 8, 0, None),
    ],
    ids=["drop_ge_floor_alerts", "throttled_across_exps", "below_floor_silent"],
)
def test_eod_continuity_alert_gate(monkeypatch, n_exps, keep, expected_alerts, drop_in_msg):
    """② 连续性过滤告警闸三态（原 3 个同构用例参数化合并，2026-08-19 W3）。

    物理意图：_eod 的 filter_universe_by_continuity 过滤掉漏采标的后，若过滤数 ≥
    _CONTINUITY_FILTER_ALERT_FLOOR（=5）→ _alert_critical 告警人工介入；_continuity_alerted_this_eod
    局部变量保单次 _eod 至多 1 条（N exp 不风暴）；阈值以下只 warning 不刷屏钉钉。

    Why patch data.integrity 而非 engine：_eod 内 `from data.integrity import
    filter_universe_by_continuity` 是函数内 local import，每次执行重新 bind 到
    data.integrity 当前属性 → patch data.integrity 命中（patch engine 不命中）。
    """
    from experiment.models import ActiveExperiment

    fake_exps = [
        ActiveExperiment(experiment_id=f"exp-cont-{i}", strategy_name="neckline",
                         params={"window": 20}, weight=1.0)
        for i in range(n_exps)
    ]
    monkeypatch.setattr(
        "experiment.resolver.resolve_active", lambda db_path=None: fake_exps
    )

    class _MockStrategy:
        def __init__(self, *a, **kw):
            pass

        def scan_live(self, symbol, df_upto, date):
            return []  # 信号生成不在本测试范围

    monkeypatch.setattr(
        "strategies.registry.build_strategy",
        lambda name, cfg_override=None, **kw: _MockStrategy(),
    )

    universe = [f"{i:06d}.SZ" for i in range(10)]
    monkeypatch.setattr(engine, "_load_universe", lambda lake: list(universe))
    monkeypatch.setattr(engine, "_load_df_upto", lambda lake, s, d: pd.DataFrame(
        {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000},
        index=pd.date_range("2026-01-01", periods=80, freq="D"),
    ))

    import data.integrity as _di
    monkeypatch.setattr(
        _di, "filter_universe_by_continuity",
        lambda univ, df_map, window, susp, trade_days: list(univ[:keep]),
    )

    captured_alerts: list[str] = []
    monkeypatch.setattr(engine, "_alert_critical", lambda msg: captured_alerts.append(msg))

    async def _fake_eod_plan(date, signals, atr_map, capital):
        return {"date": date, "n_orders": 0, "mode": "dry_run"}

    monkeypatch.setattr(engine, "eod_plan", _fake_eod_plan)

    asyncio.run(engine.TradingEngine()._eod())

    assert len(captured_alerts) == expected_alerts, (
        f"期望 {expected_alerts} 条告警（n_exps={n_exps}, keep={keep}），实际 {captured_alerts}")
    if drop_in_msg is not None:
        assert drop_in_msg in captured_alerts[0], (
            f"告警文案应含过滤数 {drop_in_msg}，实际 {captured_alerts[0]}")
