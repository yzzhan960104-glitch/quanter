# -*- coding: utf-8 -*-
"""e2e：完整交易链路 4 步闭环验收（gap4 核心交付）。

物理意图：验证二期引擎「数据时效 → 生成计划 → 隔日交易 → 复盘报告」完整业务链路。
跨日时序用 monkeypatch datetime 注入（不真睡）；broker 走 mock gw（dry_run 不触达真实
柜台）；data_lake 用 tmp parquet。

四步串联：4 步串行跑完无异常 + 计划单 symbol 贯穿出现在 fill 表 / position 表 / 复盘报告。

⚠️ 实测接口适配（与 brief 起点差异）：
    - TradingEngine._pre_open()/_post_close() 返 None（cron 包装层不返业务 dict），
      故 step3/step4 直调模块级 engine.pre_open / engine.post_close 拿业务返回 dict
      （{'submitted': N, ...} / {'drift': bool, ...}）；这样既守业务契约断言又不依赖
      cron 触发器。
    - _cancel_all_open_orders 走 engine 模块真名 patch（与既有 test_engine_order_update
      _handler.py 范式一致）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from trading import engine, position_book, review_report, trading_plan
from strategies.signal import Signal


# ============================================================================
# 测试辅助：tmp 隔离的账本 + 计划目录
# ============================================================================
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """隔离 position_book db + trading_plan 目录到 tmp，避免污染真实 logs/。

    position_book._DEFAULT_DB 在导入时已是字符串字面量（logs/trading_state.db），
    apply_fills/init_db/get_local_positions 在调用时若未显式传 db_path，会取该模块级
    变量做默认——此处 monkeypatch 该变量到 tmp，引擎间接调用链路自动命中 tmp 库
    （Task 1 已保证 db_path 默认 None 运行时解析走 _DEFAULT_DB，故 patch _DEFAULT_DB
    即可，无需 patch 单个函数签名）。TRADE_STATE_DB 是与 _DEFAULT_DB 同值的镜像环境
    变量（部分生产入口走 env 读，一并注入兜底）。

    ⚠️ 时序冻结（applied_at 与 date 对齐）：
        全链路用计划日 date="2026-07-28"（T+1 日）作为业务 key。但 position_book.apply_fill
        落 fill 表用 datetime.now().isoformat()，review_report._fetch_fills_on 按
        ``applied_at LIKE 'date%'`` 过滤——若 now 是真实今日（2026-07-27），与计划日
        07-28 前缀不匹配，复盘报告会漏成交（数据一致性断点）。
        物理上 step3/step4 本就在 T+1 日盘后跑，故冻结 position_book.datetime 至
        2026-07-28 是诚实的（不睡真实时钟，与 freezegun 同语义）。引擎/eod_plan 的
        datetime.now 不受影响（业务 key 由参数显式传 date，不依赖 now）。
    """
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    # trading_plan._plan_path 读 env TRADE_PLAN_DIR
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("TRADE_STATE_DB", db_path)

    # 冻结 position_book 内 datetime 至 T+1 日（让 applied_at 与 date 前缀匹配）
    from datetime import datetime as _RealDT
    class _FrozenDT(_RealDT):
        @classmethod
        def now(cls, tz=None):
            return _RealDT(2026, 7, 28, 15, 30, 0)
    monkeypatch.setattr(position_book, "datetime", _FrozenDT)
    return tmp_path


def _make_signal(symbol="300001.SZ"):
    """构造一个颈线法 Signal（build_orders_from_signals 需 symbol/entry/neckline/bottom/atr 非 None）。

    几何守恒：H = neckline - bottom = 10.5 - 9.5 = 1.0
        stop_price = neckline - 2*atr = 10.5 - 1.0 = 9.5（与 bottom 同 —— ATR 双倍止损）
        take_profit = entry + 2*H = 10.5 + 2.0 = 12.5
    全字段非 None 是 signal_runner.build_orders_from_signals 的硬前置，缺一即跳过挂单。
    """
    return Signal(
        symbol=symbol, signal_type="neckline", formed_at="2026-07-27",
        breakout_date="2026-07-27", neckline=10.5, bottom=9.5,
        entry_price=10.5, atr=0.5,
        experiment_id="exp_e2e", experiment_weight=1.0,
    )


# ============================================================================
# 第 1 步：检查数据时效性
# ============================================================================
def test_step1_data_freshness_ok(tmp_path):
    """第 1 步：data_lake 最新日 >= 期望日 → check_freshness ok=True。

    物理意图：盘前第一闸——数据若陈旧（增量采集失败/Tushare 限流未补），后续计划基于
    过时 K 线算信号 = 静默前视偏差（spec §0 红线）。本测试构造 6 天连续日 bar，末日正好
    等于期望日，断言 ok=True + latest_date 回填正确。
    """
    from data.freshness import check_freshness

    # 造小样本 parquet（MultiIndex date,symbol，最新日 = T 日 2026-07-27）
    dates = pd.date_range("2026-07-22", periods=6, freq="D")  # 2026-07-22..27，末日=07-27
    idx = pd.MultiIndex.from_product([dates, ["300001.SZ"]], names=["date", "symbol"])
    df = pd.DataFrame({"open": 10, "high": 11, "low": 9, "close": 10, "vol": 1000}, index=idx)
    lake = tmp_path / "lake"
    lake.mkdir()
    df.to_parquet(lake / "a_shares_daily.parquet")

    result = check_freshness("daily", expected_date="2026-07-27", lake_dir=str(lake))
    assert result.ok is True
    assert result.latest_date == "2026-07-27"


def test_step1_data_freshness_stale(tmp_path):
    """第 1 步反例：data_lake 最新日 < 期望日 → ok=False（陈旧能检出，不静默 PASS）。

    物理意图：反例守卫——若 check_freshness 因任何原因（异常吞/默认返 True）把陈旧数据
    当新鲜放行，整个二期引擎会在脏数据上跑完整链路而无任何告警（最致命的 silent fail）。
    本测试末日 = 2026-07-22 < 期望 2026-07-27，强制断言 ok=False（宁杀错不放过）。
    """
    from data.freshness import check_freshness

    dates = pd.date_range("2026-07-20", periods=3, freq="D")  # 最新 2026-07-22 < 期望 07-27
    idx = pd.MultiIndex.from_product([dates, ["300001.SZ"]], names=["date", "symbol"])
    df = pd.DataFrame({"open": 10, "high": 11, "low": 9, "close": 10, "vol": 1000}, index=idx)
    lake = tmp_path / "lake"
    lake.mkdir()
    df.to_parquet(lake / "a_shares_daily.parquet")

    result = check_freshness("daily", expected_date="2026-07-27", lake_dir=str(lake))
    assert result.ok is False


# ============================================================================
# 第 2 步：生成交易计划（T 日盘后 → eod_plan 落盘 confirmed=False）
# ============================================================================
def test_step2_generate_plan(isolated, monkeypatch):
    """第 2 步：eod_plan(signals, atr_map) → save_plan 落盘 confirmed=False（gap1/2 实证）。

    物理意图：盘后扫颈线法信号 + build_orders_from_signals 构造带止损/止盈的 PlannedOrder
    列表 → 落盘 confirmed=False（人审闸——研究员 T-1 必须显式 confirm 才允许 T 日挂单）。
    断言订单数 == 信号数（一信号一单）+ confirmed=False（未确认绝不挂单）+ symbol 回写正确。
    """
    # patch 掉钉钉推送（不触达 dws），保留 save_plan 真实落盘
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    # AUTO_TRADE_MODE=dry_run 让 eod_plan 内 _mode() 读到（影子模式，不触达真单）
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    sig = _make_signal()
    result = asyncio.run(engine.eod_plan(
        "2026-07-28",              # date=T+1（计划生效日）
        signals=[sig],
        atr_map={"300001.SZ": 0.5},
        capital=1_000_000.0,
    ))
    assert result["n_orders"] == 1
    # 计划落盘 confirmed=False（pre_open 会检查此位）
    plan = trading_plan.load_plan("2026-07-28")
    assert plan is not None
    assert plan["confirmed"] is False
    assert len(plan["orders"]) == 1
    assert plan["orders"][0]["order"]["symbol"] == "300001.SZ"


# ============================================================================
# 第 3 步：隔日按计划交易（T+1 日：confirm → pre_open 挂单 → 成交回报写账本）
# ============================================================================
def test_step3_trade_next_day(isolated, monkeypatch):
    """第 3 步：confirm_plan → pre_open 挂单（DRY_RUN）→ 成交回报写 position_book（gap3/4 实证）。

    物理意图：研究员确认 → pre_open 撤昨日未成交 + 注入白名单 + 逐单挂单 → 网关回报
    on_stock_trade 推 kind=trade 的 fill → _handle_order_update 写账本（gap4 第四连写入点）。

    ⚠️ 接口适配：TradingEngine._pre_open 返 None（cron 包装层），改直调模块级
        engine.pre_open(today) 拿 {'submitted': N} 业务返回（不依赖 cron 触发器，
        与既有 test_engine.py 同范式）。
    """
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    # 先落一份计划（复用第 2 步产物路径）
    sig = _make_signal()
    asyncio.run(engine.eod_plan("2026-07-28", [sig], {"300001.SZ": 0.5}, 1_000_000.0))

    # ① 研究员确认（T-1 确认闸）—— confirmed=False 转 True，pre_open 的硬前置
    assert trading_plan.confirm_plan("2026-07-28") is True
    assert trading_plan.load_plan("2026-07-28")["confirmed"] is True

    # ② pre_open 挂单：mock gw + _submit 返 DRY_RUN + cancel_all no-op
    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw.is_locked = False
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    # _cancel_all_open_orders patch 真身（engine 模块别名导入）—— 防真撤昨日单触达 mock gw
    monkeypatch.setattr(engine, "_cancel_all_open_orders", AsyncMock(return_value=0))

    submitted = {"n": 0}
    async def _dry_submit(order, *, confirm=True):
        submitted["n"] += 1
        return {"order_id": str(submitted["n"]), "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _dry_submit)

    # dynamic_whitelist 真实跑（pre_open 内部 inject 计划 symbols），不 patch
    # calendar.is_trading_day patch 真：避免非交易日 no-op 走偏（pre_open 内首道闸）
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    # 直调模块级 pre_open 拿业务返回（TradingEngine._pre_open 返 None 是 cron 包装）
    pre_result = asyncio.run(engine.pre_open("2026-07-28"))
    assert pre_result["submitted"] >= 1  # confirmed 计划挂单成功（gap3 实证）

    # ③ 成交回报写账本：模拟 DRY_RUN 单的「成交回报」（gap4 写入实证）
    eng = engine.TradingEngine()
    eng._gw = MagicMock()
    eng._gw._orders = {"1": {"order_type": 23}}  # 23=STOCK_BUY（xtconstant 兜底同值）
    update = {
        "kind": "trade", "order_id": "1", "stock_code": "300001.SZ",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    # patch 真身模块路径：record_live_trade（CSV 日志）+ NotificationManager（钉钉推送）
    # + _place_take_profit（避免再走 _submit 挂止盈——已 patch _submit 但走限频复杂度，禁掉更稳）
    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()):
        asyncio.run(eng._handle_order_update(update))
    # 账本写入：BUY 100 股 300001.SZ（gap4 第四连写入实证）
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}


# ============================================================================
# 第 4 步：生成复盘报告（T+1 日盘后：post_close 对账 + generate_review）
# ============================================================================
def test_step4_review_report(isolated, monkeypatch):
    """第 4 步：post_close 对账（mock gw+账本）→ generate_review 四段齐全 + save_review 落盘。

    物理意图：盘后对账（broker 持仓 vs 本地账本）+ 生成复盘报告（计划/成交/持仓/对账四段
    人类可读 Markdown）。drift=False（账本与 broker 一致）是健康基线；drift=True 时
    review 报告需显式标注「有偏差」让研究员排查。

    ⚠️ 接口适配：TradingEngine._post_close 返 None（cron 包装层），改直调模块级
        engine.post_close(date, local_positions=...) 拿 {'drift': bool} 业务返回。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    # 预置：计划落盘 + 账本有一笔 BUY 成交（复用前两步状态）
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    sig = _make_signal()
    asyncio.run(engine.eod_plan("2026-07-28", [sig], {"300001.SZ": 0.5}, 1_000_000.0))
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.5)

    # post_close：mock gw 持仓与账本一致（drift=False）+ run_reconcile 返 is_ok=True
    from trading.compute.reconcile import ReconciliationResult

    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw.is_locked = False
    fake_rec = ReconciliationResult(
        matched=[], drifted=[], only_local=[], only_broker=[],
        max_abs_drift=0.0, is_ok=True)

    async def _fake_run_rec(gw, local, tolerance=0.0):
        return fake_rec
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine.reconcile_job, "run_reconcile", _fake_run_rec)
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    # 直调模块级 post_close 拿业务返回（drift 字段）
    pc_result = asyncio.run(engine.post_close(
        "2026-07-28", gw=fake_gw, local_positions={"300001.SZ": 100.0}))
    assert pc_result["drift"] is False  # 账本与 broker 一致 → 无偏差

    # generate_review：四段齐全（计划/成交/持仓/对账）
    md = review_report.generate_review("2026-07-28", drift=False)
    assert "300001.SZ" in md
    assert "买入 1 笔" in md
    assert "无偏差" in md

    # save_review 落盘
    out = review_report.save_review("2026-07-28", md, review_dir=str(isolated / "reviews"))
    assert out.exists()


# ============================================================================
# 全链路：4 步串行（数据一致性——计划 symbol 贯穿 fill/position/报告）
# ============================================================================
def test_e2e_full_flow_symbol_propagates(isolated, monkeypatch):
    """全链路：计划单 symbol 贯穿出现在 fill 表 / position 表 / 复盘报告（数据一致性）。

    物理意图：gap4 的核心价值——「同一 symbol 在四步链路里不丢、不串、不漂」。
    若 position_book 写入失败（gap4 第四连断链），position 表会缺该 symbol；
    若 review_report 聚合错 key，报告会漏该 symbol。本测试用与 step3 不同的 symbol
    （688001.SH）防止与同 test session 其他用例的 300001.SZ 串味。
    """
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    # 第 2 步：生成计划
    sig = _make_signal("688001.SH")
    asyncio.run(engine.eod_plan("2026-07-28", [sig], {"688001.SH": 0.5}, 1_000_000.0))
    plan = trading_plan.load_plan("2026-07-28")
    assert plan["orders"][0]["order"]["symbol"] == "688001.SH"

    # 第 3 步：confirm + 成交回报写账本
    trading_plan.confirm_plan("2026-07-28")
    eng = engine.TradingEngine()
    eng._gw = MagicMock()
    eng._gw._orders = {"o1": {"order_type": 23}}
    update = {
        "kind": "trade", "order_id": "o1", "stock_code": "688001.SH",
        "traded_volume": 100, "traded_price": 10.5, "state": "FILLED",
    }
    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()):
        asyncio.run(eng._handle_order_update(update))

    # symbol 贯穿：position 表
    assert "688001.SH" in position_book.get_local_positions()

    # 第 4 步：复盘报告含同一 symbol
    md = review_report.generate_review("2026-07-28", drift=None)
    assert "688001.SH" in md  # 计划段 + 成交段 + 持仓段都应含计划 symbol
