# -*- coding: utf-8 -*-
"""Task 9（M4）：致命事件钉钉 CRITICAL 告警接线测试。

物理意图（spec M4 · 静默漏单消灭）：
    引擎致命事件（pre_open 漏挂 / 口径自检失败 / health_guard 重连耗尽）必须推钉钉
    CRITICAL，否则用户事后才发现漏单（[[qmt-connect-1-rootcause]] 全天锁死无告警）。
    本测试 monkeypatch NotificationManager.notify_risk_event 收集告警，断言：
      - pre_open live submitted=0 → level=CRITICAL 且 msg 含「漏挂」；
      - dry_run 不误报（避免告警风暴）；
      - 口径自检失败 → CRITICAL；
      - health_guard 连续失败超阈值 → CRITICAL；
      - 告警发送异常不阻塞主流程（fire_and_forget 抛错被吞）。

Why 独立文件而非扩 test_e2e：e2e 聚焦「正常链路四步」，本文件聚焦「致命事件告警」，
    断言 notify_risk_event 的调用口径（level/msg），与 e2e 正交不耦合。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading import engine


# ============================================================================
# 共享：monkeypatch notify_risk_event 收集调用，替代真实钉钉推送
# ============================================================================
@pytest.fixture
def captured_alerts(monkeypatch):
    """收集 notify_risk_event 调用（msg, level），断言用。

    I-2 修复（消除 sleep 竞态）：patch ``infra.notifier.fire_and_forget`` 改同步 await——
    engine._alert_critical 内部 ``from infra.notifier import fire_and_forget`` 是函数级
    懒 import，每次调用都重新拿模块属性，故模块级 patch 会被即时捕获。原实现起 daemon
    线程跑 ``asyncio.run(coro)``，测试需 ``time.sleep`` 等线程——CI/慢机高负载下假阴性。
    改同步执行后，_alert_critical 返回时协程已跑完，断言零竞态。
    """
    fired: list[tuple[str, str]] = []

    async def _fake_notify(self, msg, level="INFO"):
        fired.append((msg, level))
        return []

    # patch 真实模块路径：notify_risk_event 是 instance method，get_default 返单例实例。
    # 两步 patch 同步：notify_risk_event 在类上 patch（任何实例调用都进 _fake_notify），
    # get_default 返一个真实 NotificationManager 实例（其 notify_risk_event 已被类级 patch 覆盖）。
    monkeypatch.setattr(
        "infra.notifier.NotificationManager.notify_risk_event", _fake_notify)
    from infra.notifier import NotificationManager as _NM
    monkeypatch.setattr(
        "infra.notifier.NotificationManager.get_default",
        classmethod(lambda cls: _NM.__new__(_NM)))

    # fire_and_forget 改「同步阻塞跑协程」：消除 daemon 线程竞态（原 time.sleep(0.3) 等线程
    # 在 CI 高负载下可能假阴性）。_alert_critical 的 from-import 会取到这个 patch 后的符号。
    #
    # 实现关键：_alert_critical 既可能在「同步上下文」被调（eng.start() 走纯同步路径），
    # 也可能在「已运行事件循环内」被调（asyncio.run(engine.pre_open)/_health_guard）。
    # 后者场景下当前线程已绑 running loop，再调 asyncio.run 会抛
    # "asyncio.run() cannot be called from a running event loop"。故显式开一个独立 daemon
    # 线程跑 asyncio.run 并 join() 阻塞至协程完成——同步语义（调用返回即已 append 到 fired）
    # 且跨两种上下文都零竞态。线程创建成本可忽略（每测试仅 1~2 次告警）。
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


# ----------------------------------------------------------------------------
# 公共 fixture：隔离 state_store / position_book DB（C-4 U3a account L1 停调度）
# ----------------------------------------------------------------------------
# 物理意图（测试卫生 · C-4 U6 发现的退化）：
#   C-4 U3a 把 pre_open 的 account 行 upsert（FK 源）从 L3 软降级（catch + log + 继续）
#   升为 L1 硬抛 _CriticalHalt（spec §3：account 失败=后续所有 FK 写全失效=DB 真故障）。
#   原测试无 DB 隔离，靠「软降级吞 no-such-table」假性通过——U3a 后此路径直接停调度，
#   pre_open 在到「submitted=0 漏挂」告警断言点前就 raise _CriticalHalt 了。修法（最小改动，
#   不回退 U3a 语义）：autouse 隔离 state_store._DEFAULT_DB 到 tmp_path + init_store，
#   让 account upsert 正常成功，pre_open 走完主路径到「submit 全拒 → 漏挂告警」。
@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    from trading import state_store, position_book
    db_path = str(tmp_path / "alerts_state.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    state_store.init_store()


# ============================================================================
# 事件点 ① pre_open submitted=0 + live → CRITICAL（核心断言）
# ============================================================================
def test_pre_open_zero_submit_live_alerts_critical(_isolate_trade_env, monkeypatch, tmp_path, captured_alerts):
    """live 模式 pre_open submitted=0（网关拒所有单）→ 钉钉 CRITICAL。

    构造场景：gw lock_down（is_locked=True）→ _submit 抛 RuntimeError（trading_service
    契约：非 dry_run 挡板命中 raise）→ 逐单 try-except 吞掉 → submitted=0。
    断言：notify_risk_event 被调，level=CRITICAL，msg 含「漏挂」/「submitted=0」。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    # Task 8（C-2 S3）：重置 _ACTIVE_ENGINE——本测焦点是 pre_open 末尾「submitted=0 漏挂」
    # 告警，不是 gate。模块级 _ACTIVE_ENGINE 可能从前序测试泄漏（构造 TradingEngine 残留），
    # 不重置会让 pre_open 入口的三段式 gate 先拦截早返，到不了 submit 逻辑。
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", None)
    # I-1（测试卫生）：TRADE_PLAN_DIR 指向 tmp_path，save_plan/confirm_plan 落到临时目录，
    # 不污染实盘 logs/trading_plans/（_plan_path 读 os.getenv("TRADE_PLAN_DIR")）。
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))

    # 落一份已确认计划（pre_open 必须有 orders 才有「漏挂」语义——0 orders 是正常无计划，
    # 不应触发 CRITICAL）。直 save_plan 跳过 build_orders_from_signals（本测聚焦告警点，
    # 非 signal 构造），写两条订单确保 len(orders)>0。
    # C2c：pre_open 直读 DB SIGNAL.meta，需先落 SIGNAL 再 confirm（顺序保证 latest=CONFIRMED）
    from trading import trading_plan, state_store
    import json as _json
    orders = [
        {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
         "stop_price": 9.0, "take_profit": 11.0, "neckline": 10.0, "atr": 0.5,
         "formed_at": "2026-07-25", "max_wait": 5, "tp1": None, "tp1_portion": None,
         "cancel_on": None, "experiment_id": None, "experiment_weight": 1.0, "rr": 1.0},
        {"order": {"symbol": "300002.SZ", "qty": 100, "side": "buy", "price": 20.0},
         "stop_price": 18.0, "take_profit": 22.0, "neckline": 20.0, "atr": 0.5,
         "formed_at": "2026-07-25", "max_wait": 5, "tp1": None, "tp1_portion": None,
         "cancel_on": None, "experiment_id": None, "experiment_weight": 1.0, "rr": 1.0},
    ]
    trading_plan.save_plan("2026-07-28", orders)
    # C2c：落 DB SIGNAL（先于 confirm_plan）
    _aid = engine._resolve_account_id()
    if state_store.get_account(_aid) is None:
        state_store.upsert_account(_aid, broker="qmt")
    for o in orders:
        sym = o["order"]["symbol"]
        tid = state_store.build_trade_id(_aid, sym, "2026-07-28")
        meta_obj = {**o, "plan_date": "2026-07-28", "strategy_name": "neckline",
                    "rationale": ""}
        state_store.insert_trade_event(
            _aid, tid, sym, "SIGNAL",
            meta=_json.dumps(meta_obj, ensure_ascii=False))
    assert trading_plan.confirm_plan("2026-07-28") is True

    # gw 锁死：_submit 全部 raise（模拟 lock_down 拒所有单）
    fake_gw = MagicMock()
    fake_gw._connected = True
    # M-2（fake_gw async 契约）：pre_open 内 await gw.query_asset()——MagicMock 默认返非
    # awaitable，await 会抛 TypeError 被外层 try/except 吞（靠兜底过）。显式 AsyncMock 让
    # 路径干净、不依赖异常吞咽，断言聚焦真正的「漏挂」告警点。
    fake_gw.query_asset = AsyncMock(return_value={})
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine, "_cancel_all_open_orders",
                        AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0}))
    monkeypatch.setattr(engine, "_submit", AsyncMock(side_effect=RuntimeError("网关锁死")))
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    result = asyncio.run(engine.pre_open("2026-07-28"))
    assert result["submitted"] == 0  # 全部被拒

    # I-2：fire_and_forget 已在 fixture patch 为同步执行，_alert_critical 返回即已投递，
    # 无需 time.sleep 等线程。

    # 核心断言：至少一条 CRITICAL 且 msg 含「漏挂」/「submitted=0」
    critical = [(m, l) for m, l in captured_alerts if l == "CRITICAL"]
    assert critical, f"expected CRITICAL alert, got {captured_alerts}"
    assert any("漏挂" in m or "submitted=0" in m for m, _ in critical), \
        f"msg 应含「漏挂」/「submitted=0」，实际：{critical}"


def test_pre_open_zero_submit_dry_run_no_alert(_isolate_trade_env, monkeypatch, tmp_path, captured_alerts):
    """dry_run 模式 pre_open submitted=0 不触发 CRITICAL（避免告警风暴）。

    dry_run 是影子模式（验证用），submitted=0 多半是 DRY_RUN 状态被误判或 mock 问题，
    不是真「网关锁死」——真锁死只在 live 下才有漏单风险。故 dry_run 不告警。
    """
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    # Task 8（C-2 S3）：重置 _ACTIVE_ENGINE（同 live 用例，避免 gate 拦截到不了 submit 逻辑）。
    monkeypatch.setattr(engine, "_ACTIVE_ENGINE", None)
    # I-1（测试卫生）：TRADE_PLAN_DIR 指向 tmp_path，落盘不污染实盘 logs/trading_plans/。
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    from trading import trading_plan, state_store
    import json as _json
    orders = [
        {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
         "stop_price": 9.0, "take_profit": 11.0, "neckline": 10.0, "atr": 0.5,
         "formed_at": "2026-07-25", "max_wait": 5, "tp1": None, "tp1_portion": None,
         "cancel_on": None, "experiment_id": None, "experiment_weight": 1.0, "rr": 1.0},
    ]
    trading_plan.save_plan("2026-07-28", orders)
    # C2c：落 DB SIGNAL（先于 confirm_plan，保证 latest=CONFIRMED）
    _aid = engine._resolve_account_id()
    if state_store.get_account(_aid) is None:
        state_store.upsert_account(_aid, broker="qmt")
    for o in orders:
        sym = o["order"]["symbol"]
        tid = state_store.build_trade_id(_aid, sym, "2026-07-28")
        state_store.insert_trade_event(
            _aid, tid, sym, "SIGNAL",
            meta=_json.dumps({**o, "plan_date": "2026-07-28", "strategy_name": "neckline",
                              "rationale": ""}, ensure_ascii=False))
    assert trading_plan.confirm_plan("2026-07-28") is True

    fake_gw = MagicMock()
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(engine, "_cancel_all_open_orders",
                        AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0}))
    # dry_run 下 _submit 返 DRY_RUN 不 raise——构造返 REJECTED 模拟「未挂成功」
    monkeypatch.setattr(engine, "_submit",
                        AsyncMock(return_value={"state": "REJECTED", "message": "挡板"}))
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)

    asyncio.run(engine.pre_open("2026-07-28"))
    # I-2：fire_and_forget 同步执行，无需 sleep。
    # dry_run 不应触发 CRITICAL（漏挂在 live 才致命）
    assert not any(l == "CRITICAL" for _, l in captured_alerts), \
        f"dry_run 不应告警 CRITICAL，实际：{captured_alerts}"


# ============================================================================
# 事件点 ② 口径自检失败 → CRITICAL（start() 内）
# ============================================================================
def test_sanity_check_fail_alerts_critical(monkeypatch, captured_alerts):
    """口径自检失败（next_trading_day 返 today）→ start() 触发 CRITICAL 告警。

    构造：next_trading_day 返 today 自身（旧 bug 口径）→ _sanity_check_date_alignment
    返 False → start() 内补 _alert_critical。
    断言：notify_risk_event 被调 level=CRITICAL，msg 含「口径自检」。
    """
    # patch scheduler.start 不阻塞（start 会真起 scheduler，测试需 patch）
    eng = engine.TradingEngine()
    monkeypatch.setattr(eng.sched, "start", lambda: None)
    monkeypatch.setattr(engine.calendar, "next_trading_day", lambda d: d)  # 返 today（口径坏）
    # Fix1：口径自检 _alert_critical 加了 live 守卫，dry_run 模式不推钉钉。
    # 本测试断「推 CRITICAL」必须 patch _mode=live 才能命中守卫。
    monkeypatch.setattr(engine, "_mode", lambda: "live")

    eng.start()
    # I-2：fire_and_forget 同步执行，start() 返回即已投递。
    critical = [(m, l) for m, l in captured_alerts if l == "CRITICAL"]
    assert critical, f"expected CRITICAL for sanity fail, got {captured_alerts}"
    assert any("口径自检" in m for m, _ in critical), \
        f"msg 应含「口径自检」，实际：{critical}"


# ============================================================================
# 事件点 ③ health_guard 重连累计失败超阈值 → CRITICAL
# ============================================================================
def test_health_guard_fail_threshold_alerts_critical(monkeypatch, captured_alerts):
    """health_guard 连续失败超阈值（fail_count % 10 == 0）→ CRITICAL 告警。

    构造：gw 未连接 + is_client_ready=True + connect 连续抛异常，跑多轮触发 fail_count
    累加到 10 的倍数 → _alert_critical 被调。
    """
    eng = engine.TradingEngine()
    fake_gw = MagicMock()
    fake_gw._connected = False
    fake_gw._risk_halted = False  # #6：风控熔断标志默认 False（网络断线自愈路径）
    fake_gw._reconnecting = False
    fake_gw.is_client_ready = MagicMock(return_value=True)
    fake_gw.connect = AsyncMock(side_effect=RuntimeError("connect 失败"))
    monkeypatch.setattr(engine, "get_gateway", lambda: fake_gw)
    # 退避会让连续轮被跳过（skip=7 后每轮被 ⑤ 退避门拦），patch _guard_skip_rounds 返 0
    # 强制每轮都试（退避调度本身有 _guard_skip_rounds 的单元语义，不在此重复验）。
    monkeypatch.setattr(engine.TradingEngine, "_guard_skip_rounds",
                        staticmethod(lambda fail_count: 0))
    # Fix1：health_guard 重连失败 _alert_critical 加了 live 守卫，dry_run 模式不推钉钉。
    # 本测试断「推 CRITICAL」必须 patch _mode=live 才能命中守卫。
    monkeypatch.setattr(engine, "_mode", lambda: "live")

    # 跑 10 轮（每轮 fail_count+1），第 10 轮 fail_count=10 应触发 CRITICAL。
    for _ in range(10):
        asyncio.run(eng._health_guard())

    # I-2：fire_and_forget 同步执行，循环结束即已投递。
    critical = [(m, l) for m, l in captured_alerts if l == "CRITICAL"]
    assert critical, f"expected CRITICAL for health_guard exhaust, got {captured_alerts}"
    assert any("health_guard" in m and "10" in m for m, _ in critical), \
        f"msg 应含 health_guard + 失败次数，实际：{critical}"


# ============================================================================
# 鲁棒性：_alert_critical 异常不阻塞主流程
# ============================================================================
def test_alert_critical_swallows_exception(monkeypatch):
    """notify_risk_event 抛异常 → _alert_critical 吞掉不向外抛（主流程不阻塞）。

    红线：告警是「尽最大努力」，失败不能拖垮 pre_open/start 主路径——否则告警系统
    反而成为单点故障源（反讽）。
    """
    async def _boom(self, msg, level="INFO"):
        raise RuntimeError("钉钉推送爆炸")
    monkeypatch.setattr(
        "infra.notifier.NotificationManager.notify_risk_event", _boom)
    # 不应抛
    engine._alert_critical("测试告警（应被吞）")
