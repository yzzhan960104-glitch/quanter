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
from strategies.neckline.signal import Signal


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
    # state_store 与 position_book 共用同一 db（state-store-redesign 后 engine 四触发点查
    # state_store 的 trade_event/order/account 表）。patch _DEFAULT_DB + init_store 建 6 张表。
    from trading import state_store
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    state_store.init_store()
    # trading_plan._plan_path 读 env TRADE_PLAN_DIR
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("TRADE_STATE_DB", db_path)

    # Task 8（C-2 S3）：重置模块级 _ACTIVE_ENGINE 单例——pre_open 入口的三段式 gate 经它
    # 调用实例方法，若不重置会从前序测试泄漏（构造 TradingEngine 残留），让本文件里非 gate
    # 焦点的 e2e pre_open 流（step3/e2e_*）被 gate ③ 段（data_ready 无记录）拦截早返。
    # 重置为 None 后 pre_open 走防御性分支跳过 gate，保留这些测试原本验证的全链路逻辑。
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", None)

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
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.5, "t1")

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


# ============================================================================
# Task 10：韧性系统四断点场景 e2e 收口
#
# 物理意图（spec M1-M4 端到端串联）：
#     前面 Task 1-9 的单测覆盖了各组件行为（_confirm_cancelled / _health_guard /
#     _sanity_check_date_alignment / _alert_critical），但韧性系统的真实价值在于
#     「多组件联动救场」——网关锁死时漏挂告警 + 守护 job 自愈恢复 + 下一轮正常挂单
#     这条端到端链路。本节聚焦「整链路串联」，不重复单测。
#
#     - 场景②（重连恢复 e2e）：核心补，单测无整链路覆盖。完整串联
#       gw 锁死→pre_open 漏挂 CRITICAL→_health_guard 探测就绪→connect 成功→
#       _lock_down=False 恢复 live→下次 pre_open 正常挂单。
#     - 场景③（撤单确认端到端）：pre_open 内 _cancel_all_open_orders →
#       gw._confirm_cancelled 返 True 计入 cancelled / 返 False 计 unconfirmed 的
#       端到端串联（单测只测 cancel_all_open_orders 组件，不串 pre_open）。
#     - 场景④（标的口径端到端）：口径自检通过 + eod_plan 落盘 next_trading_day +
#       次日 pre_open load_plan(today) 拿到正确计划（单测只测 _sanity_check 返 bool）。
#     - 场景①（锁死漏挂+告警）：T9 test_engine_alerts.py 已充分覆盖（pre_open
#       submitted=0 → CRITICAL），e2e 视角其价值被场景②前置阶段吸收（场景②的
#       第一阶段正是「锁死→漏挂→CRITICAL」），故不单列重复测试（遵循 brief 避免重复）。
# ============================================================================


# ----- 共享：韧性 e2e 的告警收集 fixture（复用 T9 同步执行语义，消除线程竞态）-----
@pytest.fixture
def captured_alerts_e2e(monkeypatch):
    """韧性 e2e 告警收集（与 T9 captured_alerts 同口径，独立命名避免与单测 fixture 混淆）。

    Why 同步执行 fire_and_forget：_alert_critical 起默认 daemon 线程跑 asyncio.run(coro)，
    测试断言时需 time.sleep 等线程——CI 高负载下假阴性。改同步执行后，
    _alert_critical 返回即已 append 到 fired，断言零竞态（详见 T9 fixture 注释）。
    """
    fired: list[tuple[str, str]] = []

    async def _fake_notify(self, msg, level="INFO"):
        fired.append((msg, level))
        return []

    monkeypatch.setattr(
        "infra.notifier.NotificationManager.notify_risk_event", _fake_notify)
    from infra.notifier import NotificationManager as _NM
    monkeypatch.setattr(
        "infra.notifier.NotificationManager.get_default",
        classmethod(lambda cls: _NM.__new__(_NM)))

    def _sync_fire(coro):
        import threading
        box: dict = {}

        def _runner():
            try:
                asyncio.run(coro)
                box["ok"] = True
            except Exception as exc:
                box["exc"] = exc
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if "exc" in box:
            raise box["exc"]
    monkeypatch.setattr("infra.notifier.fire_and_forget", _sync_fire)
    return fired


def _save_confirmed_plan(date: str, symbols: list[str]) -> None:
    """直写一份已确认计划（跳过信号构造，聚焦韧性链路而非颈线法）。

    Why 直 save_plan 跳 eod_plan：韧性 e2e 的断言点是「网关锁死/恢复/撤单确认/口径」，
    非「信号→订单」构造；直写两条订单让 pre_open 有 orders 可挂（len(orders)>0 才有
    「漏挂」语义），与 T9 test_engine_alerts.py 同范式。
    """
    orders = [
        {"order": {"symbol": sym, "qty": 100, "side": "buy", "price": 10.0},
         "stop_price": 9.0, "take_profit": 11.0, "neckline": 10.0, "atr": 0.5,
         "formed_at": "2026-07-25", "max_wait": 5, "tp1": None, "tp1_portion": None,
         "cancel_on": None, "experiment_id": None, "experiment_weight": 1.0, "rr": 1.0}
        for sym in symbols
    ]
    trading_plan.save_plan(date, orders)
    assert trading_plan.confirm_plan(date) is True


# ============================================================================
# 场景②（核心）：网关锁死→漏挂 CRITICAL→守护 job 自愈→恢复 live→下次正常挂单
# ============================================================================
def test_e2e_lockdown_recover_full_cycle(isolated, monkeypatch, captured_alerts_e2e):
    """场景② 端到端：网关锁死→pre_open 漏挂 CRITICAL→_health_guard 自愈→恢复 live→下次 pre_open 正常挂单。

    端到端链路（与单测的差异 = 多组件联动 + 时间序列「锁死→恢复」状态机）：
        ① gw._lock_down=True / _submit raise（live 挡板拒所有单）
           → pre_open submitted=0 → 钉钉 CRITICAL（场景①前置阶段，融入不重复）
        ② 同一进程 eng._health_guard 探测 is_client_ready=True
           → 调 gw.connect() 成功 → gw._lock_down=False / _connected=True 恢复 live
        ③ 再次 pre_open：gw 已恢复 → _submit 返 DRY_RUN（不再 raise）
           → submitted>0 + 无新 CRITICAL（恢复后不该再误告警）

    与单测差异（Why e2e 价值）：
        - T9 单测只覆盖「pre_open submitted=0 → CRITICAL」一个事件点；
        - T8 单测只覆盖「_health_guard 失败累计告警」一条路径，未覆盖「connect 成功恢复 live」
          的正向恢复路径，更未串联「恢复后下次 pre_open 正常挂单」；
        - 本测试串联三阶段时间序列，验证韧性系统的核心承诺：**自愈后链路真能恢复下单**，
          而非只是「告警了事」。这是单测无法替代的端到端价值。
    """
    # 全程 live 模式（韧性场景只在 live 才有「漏挂」致命语义；dry_run 影子无风险）
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    _save_confirmed_plan("2026-07-28", ["300010.SZ", "300011.SZ"])

    # 用一个可变 dict 模拟 gw 状态机：初始锁死，守护 job connect 后翻转 _lock_down/_connected
    gw_state = {"lock_down": True, "connected": False}

    fake_gw = MagicMock()
    fake_gw._reconnecting = False
    fake_gw.query_asset = AsyncMock(return_value={})  # 熔断基线兜底（返空跳过 snapshot）

    def _is_client_ready(staleness_sec: int = 300):
        # 客户端就绪探测：守护 job 据此决定是否空跑。始终 True 让 _health_guard 进入 connect 路径
        return True
    fake_gw.is_client_ready = _is_client_ready

    async def _connect():
        # connect 成功：翻转 gw 状态（_lock_down=False / _connected=True 恢复 live）
        gw_state["lock_down"] = False
        gw_state["connected"] = True
        fake_gw._lock_down = False
        fake_gw._connected = True
    fake_gw.connect = AsyncMock(side_effect=_connect)

    def _prop(name):
        # 属性代理 gw_state（MagicMock 默认属性返新 MagicMock，须显式锚定布尔语义）
        return gw_state[name]
    # 用 side_effect 让每次属性访问都返当前状态（而非 MagicMock 创建时的快照）
    fake_gw._lock_down = True
    fake_gw._connected = False

    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine, "_cancel_all_open_orders",
                        AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0}))

    # 阶段①：gw 锁死 → _submit 全 raise（live 挡板拒单契约）→ pre_open submitted=0 + CRITICAL
    submit_calls_phase1 = {"n": 0}

    async def _submit_lockdown(order, *, confirm=True):
        submit_calls_phase1["n"] += 1
        raise RuntimeError("网关锁死拒单（live 挡板）")
    monkeypatch.setattr(engine, "_submit", _submit_lockdown)

    # 同步 _lock_down 属性到 gw_state（阶段①锁死态）
    fake_gw._lock_down = True
    fake_gw._connected = False
    pre1 = asyncio.run(engine.pre_open("2026-07-28"))
    assert pre1["submitted"] == 0  # 全部被拒
    assert submit_calls_phase1["n"] == 2  # 两条订单都尝试了（逐单兜底，不炸整批）
    # 场景①前置断言：live 漏挂触发 CRITICAL（融入场景②，不单列重复 T9 单测）
    critical_phase1 = [(m, l) for m, l in captured_alerts_e2e if l == "CRITICAL"]
    assert critical_phase1, "阶段①锁死应触发 CRITICAL 漏挂告警"
    assert any("漏挂" in m or "submitted=0" in m for m, _ in critical_phase1)

    # 阶段②：守护 job _health_guard 探测就绪 → connect 成功 → 恢复 live
    #     构造一个独立 TradingEngine 实例跑守护 job（与 pre_open 模块函数共享同一 fake_gw
    #     via get_gateway patch），验证守护 job 真能翻转 gw 锁死态。
    eng = engine.TradingEngine()
    # patch 退避返 0 强制每轮都试（退避调度单测已覆盖，本 e2e 聚焦恢复链路不重复验退避）
    monkeypatch.setattr(engine.TradingEngine, "_guard_skip_rounds",
                        staticmethod(lambda fail_count: 0))
    # 此时 fake_gw._connected=False → 守护 job 进入 connect 路径
    asyncio.run(eng._health_guard())
    # 恢复成功：gw 状态翻转（_lock_down=False / _connected=True）
    assert gw_state["lock_down"] is False, "守护 job 应解除 lock_down 恢复 live"
    assert gw_state["connected"] is True, "守护 job 应置 _connected=True"
    # 守护 job 成功路径不应误触发 health_guard 失败告警（恢复是好事，不该告警）
    health_critical = [m for m, l in captured_alerts_e2e
                       if l == "CRITICAL" and "health_guard" in m]
    assert not health_critical, "恢复成功不应触发 health_guard 失败告警"

    # 阶段③：再次 pre_open → gw 已恢复 → _submit 返 DRY_RUN（不再 raise）→ submitted>0
    #     换一份新日期计划避免与阶段①的 max_wait 窗口过滤冲突（formed_at=07-25，日期近）
    _save_confirmed_plan("2026-07-29", ["300012.SZ"])
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    submit_calls_phase3 = {"n": 0}

    async def _submit_recovered(order, *, confirm=True):
        submit_calls_phase3["n"] += 1
        # 恢复后挂单成功（live 模式下真单返 OrderState.name 字符串）
        return {"order_id": str(submit_calls_phase3["n"]), "state": "SUBMITTED", "message": "ok"}
    monkeypatch.setattr(engine, "_submit", _submit_recovered)

    # Task 8（C-2 S3）：本测焦点是韧性恢复链路，不是 gate。前序 eng=TradingEngine()
    # 已把 _ACTIVE_ENGINE 置位，此处重置回 None 让 pre_open 跳过三段式 gate（data_ready
    # 表在 e2e 隔离环境无记录，gate ③ 段会拦截），保留原本验证的「恢复后正常挂单」逻辑。
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", None)
    pre3 = asyncio.run(engine.pre_open("2026-07-29"))
    assert pre3["submitted"] >= 1, "恢复后下次 pre_open 应正常挂单（韧性核心承诺）"
    assert submit_calls_phase3["n"] == 1
    # 阶段③成功挂单不应再触发新的漏挂 CRITICAL（恢复后不该误告警）
    new_critical_after_recover = [(m, l) for m, l in captured_alerts_e2e
                                  if l == "CRITICAL" and ("漏挂" in m or "submitted=0" in m)
                                  and "2026-07-29" in m]
    assert not new_critical_after_recover, "恢复后正常挂单不应触发新漏挂告警"


# ============================================================================
# 场景③（端到端串联）：pre_open → _cancel_all_open_orders → _confirm_cancelled 计数
# ============================================================================
def test_e2e_cancel_confirm_in_pre_open(isolated, monkeypatch):
    """场景③ 端到端：pre_open 内撤昨日遗留单 → _confirm_cancelled 返 True/False 分流计数。

    端到端链路（与单测差异 = 串联 pre_open 主路径 + 真实 _cancel_all_open_orders）：
        - gw._orders 含昨日未成交单（非终态）+ 已成交单（终态）；
        - pre_open 调 _cancel_all_open_orders（真身 trading.io.breaker）；
        - 对非终态单调 gw.cancel_order + gw._confirm_cancelled：
            * 一笔 _confirm_cancelled 返 True → 计入 cancelled（已确认终态）；
            * 一笔 _confirm_cancelled 返 False → 计入 unconfirmed（超时未确认）；
        - pre_open 据 n_unconfirmed>0 记 WARNING（不阻塞挂单主路径）。
        - 终态单（FILLED）不被重复撤。

    与单测差异：
        - test_breaker_cancel_confirm.py 单测只测 cancel_all_open_orders 组件本身，
          不串 pre_open 主路径；
        - 本测试验证 pre_open 真把 _cancel_all_open_orders 的 {cancelled, unconfirmed}
          返回值用起来了（记 WARNING 日志 + 不阻塞后续挂单），端到端串联完整。
    """
    from trading.types.order_state import OrderState

    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")  # dry_run 让 _submit 不触达真单
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    _save_confirmed_plan("2026-07-28", ["300020.SZ"])

    # 构造 gw._orders：3 笔昨日遗留单
    #   o1 非终态 + _confirm_cancelled 返 True（确认撤成）→ cancelled
    #   o2 非终态 + _confirm_cancelled 返 False（主推延迟）→ unconfirmed
    #   o3 已终态 FILLED → 不撤（cancel_all 跳过终态）
    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw._lock_down = False
    fake_gw.query_asset = AsyncMock(return_value={})
    fake_gw._orders = {
        "o1": {"state": OrderState.SUBMITTED, "order_type": 23},   # 非终态，待撤
        "o2": {"state": OrderState.PARTIAL_FILLED, "order_type": 23},  # 非终态，待撤
        "o3": {"state": OrderState.FILLED, "order_type": 23},      # 终态，跳过
    }
    fake_gw.cancel_order = AsyncMock(return_value=None)
    # state-store-redesign 后 cancel_all_open_orders 优先走柜台查询路径；
    # 本场景测【内存回退路径】（无 query_orders 的网关），显式置空让其走内存路径。
    fake_gw.query_orders = None
    fake_gw.cancel_order_by_broker_oid = None

    # _confirm_cancelled：o1 返 True，o2 返 False（按 oid 分流）
    async def _confirm(oid, timeout=5.0, interval=0.5):
        return oid == "o1"
    fake_gw._confirm_cancelled = _confirm

    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)

    # 不 patch _cancel_all_open_orders——让它真跑（端到端验证 pre_open 串了真身）
    # _submit 返 DRY_RUN 让挂单主路径正常（聚焦撤单链路断言）
    async def _dry_submit(order, *, confirm=True):
        return {"order_id": "new1", "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _dry_submit)

    # 记 WARNING 日志（pre_open 据 n_unconfirmed>0 记 WARNING，捕获日志验证串联）
    import logging
    warns: list[str] = []
    real_warning = engine.logger.warning

    def _spy_warning(msg, *args):
        warns.append(msg % args if args else msg)
        return real_warning(msg, *args)
    monkeypatch.setattr(engine.logger, "warning", _spy_warning)

    result = asyncio.run(engine.pre_open("2026-07-28"))

    # 撤单端到端断言：
    #   - cancel_order 被调 2 次（o1+o2 非终态撤，o3 终态跳过）
    assert fake_gw.cancel_order.await_count == 2, \
        f"应撤 2 笔非终态单（o3 终态跳过），实际 {fake_gw.cancel_order.await_count}"
    #   - pre_open 仍正常挂单（撤单不阻塞主路径）—— dry_run submitted>=1
    assert result["submitted"] >= 1, "撤单链路不应阻塞挂单主路径"
    #   - pre_open 日志体现「未确认 N 笔」（n_unconfirmed=1，o2 返 False）
    #     pre_open 内 unconfirmed>0 记 WARNING「pre_open 有 X 笔撤单未确认终态」
    assert any("撤单未确认" in w for w in warns), \
        f"pre_open 应记撤单未确认 WARNING，实际 warns={warns}"


# ============================================================================
# 场景④（端到端串联）：口径自检通过 + eod_plan 落盘 next_trading_day + 次日 load_plan 对齐
# ============================================================================
def test_e2e_sanity_date_alignment_loads_right_plan(isolated, monkeypatch):
    """场景④ 端到端：口径自检通过 → eod_plan 落盘 next_trading_day → 次日 pre_open load_plan(today) 拿到正确标的。

    端到端链路（与单测差异 = 串联「自检 + 落盘 key + 次日读 today」三段一致性）：
        - T 日（2026-07-27，周一）：_sanity_check_date_alignment(today=T) 通过
          （next_trading_day(T) 算出 T+1，确认不是旧 bug 口径）；
        - T 日盘后 eod_plan：落盘 key = next_trading_day(T) = T+1（2026-07-28）；
        - T+1 日（2026-07-28）盘前 pre_open：load_plan(today=T+1) 拿到 T 日落的计划
          （key 对齐，不是「无计划」跳过）。

    与单测差异：
        - test_engine_sanity_check.py 单测只断言 _sanity_check_date_alignment 返 True/False，
          不串「自检通过 → eod_plan 落盘 key → 次日 load_plan 对齐」；
        - 本测试验证 [[eod-date-offbyone-fix]] 修复后，落盘 key 与次日读 today 真的对齐了
          （旧 bug 下 eod 落 today，pre_open 读 T+1，永远差一天 → 永不挂单）。
          这是端到端的一致性断言，单测无法替代。

    Why 两个不同 symbol：避免与同 session 其他用例的 300001.SZ / 688001.SH 串味。
    """
    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    # 阶段①：T 日（2026-07-27）启动口径自检（next_trading_day 真身未 patch，算出 T+1）
    eng = engine.TradingEngine()
    ok = eng._sanity_check_date_alignment("2026-07-27")
    assert ok is True, "口径自检应通过（next_trading_day 算出 T+1，非旧 bug 口径）"

    # 阶段②：T 日盘后 eod_plan，用 next_trading_day(T) 作为落盘 key（与 _eod 同口径）
    t_day = "2026-07-27"
    plan_date = engine.calendar.next_trading_day(t_day)  # = T+1（口径自检验证过的值）
    assert plan_date != t_day, "next_trading_day 必须算出次日（自检通过的物理意义）"

    sig = _make_signal("300099.SZ")
    asyncio.run(engine.eod_plan(plan_date, [sig], {"300099.SZ": 0.5}, 1_000_000.0))
    # 落盘 key = plan_date（T+1），confirmed=False（人审闸）
    plan = trading_plan.load_plan(plan_date)
    assert plan is not None, f"计划应落在 {plan_date}（T+1，next_trading_day 口径）"
    assert plan["orders"][0]["order"]["symbol"] == "300099.SZ"

    # 阶段③：T+1 日盘前 pre_open，load_plan(today=T+1) 拿到 T 日落的计划（key 对齐）
    #     旧 bug 下：eod 落 today=T，pre_open 读 T+1 → load_plan(T+1) 返 None → reason=「无计划」
    #     修复后：eod 落 plan_date=T+1，pre_open 读 T+1 → load_plan(T+1) 命中 → 正常挂单
    trading_plan.confirm_plan(plan_date)  # 研究员 T-1 确认闸
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")

    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw._lock_down = False
    fake_gw.query_asset = AsyncMock(return_value={})
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine, "_cancel_all_open_orders",
                        AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0}))
    submitted = {"n": 0}

    async def _dry_submit(order, *, confirm=True):
        submitted["n"] += 1
        return {"order_id": str(submitted["n"]), "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _dry_submit)

    # pre_open(today=T+1)——load_plan 命中 T 日落的计划（key 对齐 = 口径自检的端到端兑现）
    # Task 8（C-2 S3）：本测焦点是口径自检，不是 gate。前序 eng=TradingEngine() 已把
    # _ACTIVE_ENGINE 置位，此处重置回 None 让 pre_open 跳过三段式 gate（data_ready 表
    # 在 e2e 隔离环境无记录，gate ③ 段会拦截），保留原本验证的口径对齐逻辑。
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", None)
    result = asyncio.run(engine.pre_open(plan_date))
    assert result["submitted"] >= 1, \
        f"次日 pre_open 应挂上 T 日落的计划（口径对齐），实际 submitted={result['submitted']}"
    assert "reason" not in result or result.get("submitted", 0) > 0, \
        "不应因 key 错位返「无计划」跳过（旧 bug 的静默致命特征）"


# ============================================================================
# T14（state-store-redesign）：全链路 e2e —— DB 6 张表数据一致性贯穿
# ============================================================================
def test_e2e_full_chain_db_consistency(isolated, monkeypatch):
    """全链路：eod_plan(SIGNAL+CONFIRMED) → pre_open(OPEN 幂等) → 成交(fill+TP1/TP2 幂等)
    → post_close(CLOSED + account_daily) —— DB 6 张表数据一致贯穿。

    物理意图（state-store-redesign §5 数据流 + §8 验收）：验证统一状态库在完整交易
    生命周期的事件流一致性：trade_event 完整事件链（SIGNAL→CONFIRMED→ORDERED→FILLED→
    CLOSED）+ order 幂等 + fill 增量 + position 汇总。
    """
    import sqlite3
    from trading import state_store

    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "true")  # 自动确认，让 pre_open 直接挂单
    # 显式置 QMT_ACCOUNT_ID，避免 .env（load_dotenv）在完整套件中污染致 account_id 漂移
    monkeypatch.setenv("QMT_ACCOUNT_ID", "e2e_test_acc")
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    account_id = engine._resolve_account_id()

    # ── ① eod_plan：落 SIGNAL + CONFIRMED 事件 ──
    sig = _make_signal()
    asyncio.run(engine.eod_plan("2026-07-28", [sig], {"300001.SZ": 0.5}, 1_000_000.0))
    trade_id = f"{account_id}_300001.SZ_2026-07-28"
    assert state_store.get_latest_action(trade_id) == "CONFIRMED"  # SIGNAL→CONFIRMED

    # ── ② pre_open：挂 OPEN 委托（幂等）+ ORDERED 事件 ──
    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw._lock_down = False
    fake_gw.query_asset = AsyncMock(return_value={"total_asset": 1_000_000.0, "cash": 500_000.0})
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine, "_cancel_all_open_orders",
                        AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0}))
    async def _dry_submit(order, *, confirm=True):
        return {"order_id": "seq1", "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _dry_submit)
    pre_result = asyncio.run(engine.pre_open("2026-07-28"))
    assert pre_result["submitted"] >= 1
    assert state_store.has_order(account_id, "2026-07-28", "300001.SZ", "OPEN") is True

    # 幂等：再跑 pre_open 不重复挂（has_order OPEN 跳过）
    sub_count = {"n": 0}
    async def _count_submit(order, *, confirm=True):
        sub_count["n"] += 1
        return {"order_id": "x", "state": "DRY_RUN"}
    monkeypatch.setattr(engine, "_submit", _count_submit)
    asyncio.run(engine.pre_open("2026-07-28"))
    assert sub_count["n"] == 0  # DB 幂等：第二次跳过

    # ── ③ 成交回报：fill + FILLED 事件 + TP1/TP2 止盈挂单（幂等）──
    eng = engine.TradingEngine()
    eng._gw = MagicMock()
    eng._gw._orders = {"seq1": {"order_type": 23}}  # BUY
    with patch("presentation.server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value.notify_trade_event = AsyncMock(return_value=[])
        trade_update = {
            "kind": "trade", "order_id": "seq1", "stock_code": "300001.SZ",
            "traded_volume": 100, "traded_price": 10.5, "traded_time": "20260728",
            "state": "FILLED",
        }
        asyncio.run(eng._handle_order_update(trade_update))
    # fill 表 + FILLED 事件 + position 汇总
    with sqlite3.connect(state_store._DEFAULT_DB) as con:
        n_fill = con.execute("SELECT COUNT(*) FROM fill WHERE symbol='300001.SZ'").fetchone()[0]
    assert n_fill == 1
    assert state_store.get_position(account_id, "300001.SZ") is not None

    # ── ④ post_close：trade_event(CLOSED) + account_daily（持仓归零场景需先卖出）──
    # 先模拟卖出平仓（position 归零）→ post_close 应标 CLOSED
    state_store.apply_fill_to_position(
        account_id, "300001.SZ", "SELL", 100, 11.0, "15:00:00")
    assert state_store.get_position(account_id, "300001.SZ") is None  # 归零
    fake_gw.query_asset = AsyncMock(
        return_value={"total_asset": 1_010_000.0, "cash": 510_000.0, "market_value": 500_000.0})
    # post_close 内 today 用 datetime.now()，account_daily 落真实今日；断言用真实今日
    from datetime import datetime as _dt_now
    _today_real = _dt_now.now().strftime("%Y-%m-%d")
    # reconcile/query_trades 用 mock 兜底（MagicMock 的 sync_positions 非 async，patch 掉）
    from trading import reconcile_job as _rj
    monkeypatch.setattr(_rj, "run_reconcile", AsyncMock(return_value=MagicMock(is_ok=True)))
    from presentation.server.services import trading_service as _svc
    monkeypatch.setattr(_svc, "query_trades", lambda *a, **k: {"trades": []})
    asyncio.run(engine.post_close("2026-07-28", gw=fake_gw, local_positions={}))
    # CLOSED 事件 + account_daily 收盘快照（date=真实今日，post_close 用 datetime.now）
    assert state_store.get_latest_action(trade_id) == "CLOSED"
    with sqlite3.connect(state_store._DEFAULT_DB) as con:
        row = con.execute(
            "SELECT close_total_asset FROM account_daily WHERE account_id=? AND date=?",
            (account_id, _today_real)).fetchone()
    assert row is not None
    assert row[0] == 1_010_000.0


# ============================================================================
# Task 10（C-2 收口）：事件链 e2e —— 采集→freshness→eod→(次日)pre_open gate 全绿挂单
# ============================================================================
def test_e2e_pipeline_then_eod_to_pre_open_gate_all_green(isolated, monkeypatch):
    """C-2 收口 e2e：pipeline_then_eod 跑采集（mock subprocess）→ 写 data_ready → eod 落 plan
    →（模拟次日）pre_open 三段式 gate 全绿 → 挂单成功。

    物理意图（C-2 spec 全链路收口 · Task 10）：
        本测试串联 C-2 全部前序 Task 的事件链——Task 6（pipeline_then_eod 编排）+
        Task 1/2（data_ready 落库 + required_data_keys 声明）+ Task 8（pre_open 三段式 gate），
        验证「采集→数据就绪落库→eod 落计划→次日盘前 gate 三段全绿放行挂单」这条端到端
        事件链真能跑通。这是单测无法替代的：Task 6 单测只验编排顺序、Task 8 单测只验 gate
        各段拦截，本测试把「编排层落库的 data_ready」真喂给「gate ③ 段的 get_data_ready」，
        验证两个 Task 的数据契约（Task 1 定义 → Task 6 写 → Task 8 读）在事件链里真对齐。

    与单测差异（Why e2e 价值）：
        - test_pipeline_then_eod.py 单测 mock 掉 engine._eod，不验「eod 落的计划真能被次日
          pre_open 读到 + gate 放行」；
        - test_engine_pre_open_gate.py 单测用 patch("...get_data_ready", return_value=...)
          直接注入读结果，不验「pipeline_then_eod 真把 data_ready 落进 DB + gate 真能从
          同一个 DB 读到」（DB 落库契约的端到端一致性）；
        - 本测试用同一个隔离的 state_store DB，让 pipeline_then_eod 的 upsert_data_ready
          与 pre_open gate 的 get_data_ready 走同一张真实表，验证落库契约端到端对齐。

    端到端链路：
        ① pipeline_then_eod：mock subprocess（rc=0）+ mock check_freshness（ok=True）
           → 写 data_ready(date, daily, ok=1) 落库（Task 1 表 + Task 6 步骤 4）
           → all_ok → 调 engine._eod()（mock 成写 confirmed 计划 + 落 SIGNAL/CONFIRMED 事件）
        ②（模拟次日）pre_open：_ACTIVE_ENGINE 置位让 gate 生效 + get_gateway 返 connected
           & ready 的 gw + _submit 返 DRY_RUN
           → gate ① 计划 confirmed ✓ / ② 网关 connected & client_ready ✓ /
             ③ get_data_ready(date, daily) 命中 ① 写的记录 ok=1 ✓
           → 三段全绿放行 → _submit 被调 → submitted>=1

    Why 冻结 pipeline_then_eod 的 datetime：pipeline_then_eod 内部 today=datetime.now()
    作为 data_ready 落库 key + eod 计划日。pre_open gate ③ 段用 pre_open(date) 的 date 查
    data_ready。两处 date 必须一致才能命中——冻结 pipeline.datetime.now() 返回固定
    PIPE_DATE，_eod mock 落计划 + confirm 用 PIPE_DATE，pre_open(PIPE_DATE) 查同一日
    data_ready，整链路 date 口径对齐（端到端验证 date 契约一致性）。
    """
    from trading.orchestrate import pipeline as pipeline_mod
    from trading import state_store

    PIPE_DATE = "2026-07-30"  # 固定事件链日（pipeline datetime.now + eod 计划日 + pre_open 日）

    monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", lambda d, o, **kw: True)
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    # AUTO_CONFIRM_PLAN=true：模拟「研究员确认闸已过」（eod_plan 落盘即 confirm），
    # 让 pre_open gate ① 段放行——本测焦点是事件链 + gate ③ 段数据就绪，不是人审流程。
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "true")
    # 显式置 QMT_ACCOUNT_ID，避免 .env（load_dotenv）在完整套件中污染致 account_id 漂移
    monkeypatch.setenv("QMT_ACCOUNT_ID", "e2e_c2_acc")
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    # 冻结 pipeline_then_eod 内 datetime.now() → 固定 PIPE_DATE（data_ready 落库 key 口径）
    from datetime import datetime as _RealDT
    class _FrozenDT(_RealDT):
        @classmethod
        def now(cls, tz=None):
            return _RealDT(2026, 7, 30, 19, 0, 0)
    monkeypatch.setattr(pipeline_mod, "datetime", _FrozenDT)

    # ── ① pipeline_then_eod：mock subprocess（rc=0）+ check_freshness（ok=True）+ eod 落计划 ──
    # 用一个可变 box 让 _eod mock 能在 pipeline 内部被调时落计划（模拟 eod_plan 的产物）
    eod_called = {"n": 0}

    class _FakeEngineForPipeline:
        """pipeline_then_eod 的 engine 参数：只需 async _eod()（编排层低耦合契约）。"""
        async def _eod(self):
            eod_called["n"] += 1
            # 模拟 eod_plan 产物：落计划 + 自动确认（模拟 AUTO_CONFIRM_PLAN）
            # 用一个真实颈线信号走 eod_plan 真身落盘（复用 _make_signal + engine.eod_plan），
            # 让计划 orders 结构与生产一致（gate ① 段读 plan["confirmed"] + _plan_data_keys
            # 反查 experiment_id），而不是直写裸 dict（避免与生产 plan 结构漂移）。
            sig = _make_signal("300077.SZ")
            await engine.eod_plan(PIPE_DATE, [sig], {"300077.SZ": 0.5}, 1_000_000.0)

    from data.freshness import FreshnessResult

    async def _fake_pipeline():
        await pipeline_mod.pipeline_then_eod(_FakeEngineForPipeline())
    # patch 掉子进程（不真起 ops/data_pipeline.py）+ freshness（不读真实 parquet）
    # + resolve_active 返空让 keys 回退默认 {daily}（聚焦事件链编排而非策略装配）
    with patch("trading.orchestrate.pipeline.asyncio.create_subprocess_exec") as cse, \
         patch("trading.orchestrate.pipeline.resolve_active", return_value=[]), \
         patch("trading.orchestrate.pipeline.check_freshness",
               return_value=FreshnessResult("daily", True, PIPE_DATE, PIPE_DATE, "PASS")), \
         patch("ops.brief_all.run_brief_all", new=AsyncMock(return_value=0)):
        proc = AsyncMock(); proc.wait.return_value = 0  # rc=0 采集成功
        cse.return_value = proc
        asyncio.run(_fake_pipeline())

    # 事件链 ① 断言：_eod 被调（all_ok → 放行 eod）+ data_ready 落库（Task 1 表 + Task 6 步骤 4）
    assert eod_called["n"] == 1, "数据全绿应放行 _eod（Task 6 编排顺序）"
    ready_row = state_store.get_data_ready(PIPE_DATE, "daily")
    assert ready_row is not None, "pipeline_then_eod 应落 data_ready 记录（Task 6 步骤 4 + Task 1 表）"
    assert ready_row["ok"] == 1, "全绿场景 data_ready.ok=1（Task 1 落库契约）"
    # eod 落的计划 confirmed=True（AUTO_CONFIRM_PLAN 模拟 + gate ① 段前置）
    plan = trading_plan.load_plan(PIPE_DATE)
    assert plan is not None and plan["confirmed"] is True
    assert plan["orders"][0]["order"]["symbol"] == "300077.SZ"

    # ── ②（模拟次日）pre_open：_ACTIVE_ENGINE 置位让 gate 生效 + gw connected&ready ──
    eng = engine.TradingEngine()
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", eng)  # gate 经单例调用实例方法（Task 4 范式）

    fake_gw = MagicMock()
    fake_gw._connected = True
    fake_gw.is_client_ready = lambda *a, **kw: True  # gate ② 段客户端就绪
    fake_gw.query_asset = AsyncMock(return_value={})  # 熔断基线兜底（返空跳过 snapshot）
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine, "_cancel_all_open_orders",
                        AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0}))

    submitted = {"n": 0}

    async def _dry_submit(order, *, confirm=True):
        submitted["n"] += 1
        return {"order_id": str(submitted["n"]), "state": "DRY_RUN", "message": "影子"}
    monkeypatch.setattr(engine, "_submit", _dry_submit)

    # pre_open(PIPE_DATE)：gate ① 计划 confirmed ✓ / ② 网关 connected&ready ✓ /
    # ③ get_data_ready(PIPE_DATE, daily) 命中 ① 写的记录 ok=1 ✓ → 三段全绿放行挂单
    result = asyncio.run(engine.pre_open(PIPE_DATE))

    # 事件链 ② 断言：三段全绿 → gate 放行 → submitted>=1（不被任何 gate 段早返 skip）
    assert "skipped" not in result, \
        f"gate 应三段全绿放行，不应早返 skip，实际 result={result}"
    assert result["submitted"] >= 1, \
        f"全绿 gate 应放行挂单（端到端事件链收口），实际 submitted={result['submitted']}"
    assert submitted["n"] == 1, "_submit 应被调一次（gate 放行后真挂单）"

